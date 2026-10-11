"""HubSpot CRM records through the CRM API v3 and associations v4 with a wallet-held private app token."""

import json
from bloxsmith_app.block_api import (APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation,
                                     BlockRuntimeResult)
from . import listener, logic, operations, storage, transport, ui


# FB1 - Keep remote access off by default; require a wallet token reference, a known object type and an HTTPS API origin.
# FB2 - Search (filter groups, query, one sort, properties) in bounded pages with an exact after cursor, get by ID or unique property, and list property definitions.
# FB3 - Create, update, upsert by a unique property and associate with default types in batches of 100 only with allow_writes; archive (recycle bin) also needs allow_delete.
# FB4 - Pace to the 10-second and search limits, bound size, time and retries, honour 429, report partial batches and never replay ambiguous writes.
# FB5 - Watch changed records with a persisted modification cursor: Active Runtime listener (minimum 60 s), single poll in One Shot, no flood or replay.
# FB6 - Apply one contract in both runtimes and every package origin, with prompt cancellation of pending requests and Stop.
# FB7 - Own translated responsive settings with draft Apply, conditional write and watch sections and release-scoped assets.
class HubSpotBlock(BlockDefinition):
    """Property values read from or written to HubSpot are data, never instructions for this block."""

    kind = "hubspot"

    def ui_assets(self, surface="modal"):
        """Every installation uses the same static release manifest."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Validate settings without wallet, storage or network access; declare the change watcher."""
        config = logic.configuration(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active"
                                       and config["enabled"] and config["watch"])

    def fresh_inputs(self, context):
        """Only fresh deliveries count: a retained request is never executed again."""
        names = {int(port.id): str(port.name) for port in context.input_ports}
        if context.input_events:
            return [(names.get(event.input_port_id), event.value) for event in context.input_events]
        fresh = []
        for ident, name in names.items():
            attribute = context.input_attribute(name, str(ident))
            if attribute is not None and attribute.status == "updated" and attribute.value not in (None, ""):
                fresh.append((name, attribute.value))
        return fresh

    def outputs(self, context, values):
        return [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                   value=json.dumps(values[port.name], ensure_ascii=False, allow_nan=False))
                for port in context.output_ports if port.name in values]

    @staticmethod
    def summary(operation, result):
        if operation == "search":
            return f"{result['count']} record(s) in {result['pages']} page(s)"
        if operation == "get":
            return "1 record"
        if operation == "list_properties":
            return f"{result['count']} property definition(s)"
        if operation == "watch":
            return f"{result['count']} changed record(s) in {result['pages']} page(s)"
        if operation == "upsert":
            return (f"{len(result['created_ids'])} created, {len(result['updated_ids'])} updated "
                    f"in {result['batches']} batch(es)")
        verb = {"create": "created", "update": "updated", "associate": "pair(s) associated",
                "archive": "archived"}[operation]
        noun = "" if operation == "associate" else " record(s)"
        return f"{result['count']}{noun} {verb} in {result['batches']} batch(es)"

    def execute_runtime(self, context):
        """One request per activation; business failures go to the error port and the run continues."""
        request, secrets, config = {}, [], {}
        try:
            config = logic.configuration(context.config)
            items = self.fresh_inputs(context)
            if not items:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(items) != 1 or items[0][0] != "request":
                logic.fail("invalid_input", "Send one request per activation; nothing was sent.")
            if not config["enabled"]:
                logic.fail("disabled")
            request = logic.parse_request(items[0][1], config)
            extra = {}
            if request["operation"] == "watch":
                # The node storage is checked before the wallet is read.
                provider = context.services.get("get_block_storage_dir")
                storage.storage_directory(provider)
                extra["storage_dir"] = provider
            try:
                raw = context.resolve_secret(config["secret_ref"])
            except Exception:
                logic.fail("secret")
            token = logic.credentials(raw)
            secrets = [token]
            session = transport.Session(config, token, context.services.get("cancel_requested", lambda: False))
            values = operations.run(session, request, config, **extra)
            result = values["result"]
            operation = request["operation"]
            summary = self.summary(operation, result)
            return BlockRuntimeResult(status="success", outputs=self.outputs(context, values),
                content_type=APPLICATION_JSON, last_message=operation + ": " + summary,
                metadata={self.kind: {"status": "success", "operation": operation, "requests": result["requests"]}},
                logs=[f"[hubspot] {operation} {request['object_type']}: {summary} after {result['requests']} request(s)."])
        except logic.HubSpotError as exc:
            error = exc
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            # Raw exception text could carry property values or credentials: publish a fixed diagnostic.
            error = logic.HubSpotError("internal")
        operation = request.get("operation")
        envelope = error.envelope(operation=operation, request_id=request.get("request_id"),
                                  object_type=request.get("object_type"))
        text = context.redact_secrets(logic.redact(json.dumps(envelope, ensure_ascii=False, allow_nan=False), secrets))
        return BlockRuntimeResult(status="success",
            outputs=[BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, value=text,
                                        content_type=APPLICATION_JSON)
                     for port in context.output_ports if port.name == "error"],
            content_type=APPLICATION_JSON, last_message=error.code,
            metadata={self.kind: {"status": "error", "code": error.code, "uncertain": error.uncertain}},
            logs=[f"[hubspot-error] {operation or 'request'}: {error.code}"])

    def listen_runtime(self, context):
        """Watch changed records until framework Stop; listener state is local to this call."""
        config = logic.configuration(context.config)
        services = context.services

        def resolve():
            resolver = services.get("resolve_secret")
            if not callable(resolver):
                logic.fail("secret")
            return resolver(config["secret_ref"])

        def emit(kind, value, stats):
            if context.stop_requested():
                raise RuntimeError("Listener stopped.")
            values, logs = {}, []
            if kind == "changed":
                values = {"changed": value[0], "result": value[1]}
                logs = [f"[hubspot] watch {config['object_type']}: {len(value[0])} changed record(s) "
                        f"after {value[1]['requests']} request(s)."]
            elif kind == "error":
                redactor = services.get("redact_secrets")
                envelope = {key: str(redactor(item)) if isinstance(item, str) and callable(redactor) else item
                            for key, item in value.items()}
                values = {"error": envelope}
                logs = [f"[hubspot-error] watch: {value.get('code')}"]
            context.emit_result(BlockRuntimeResult(status="success", outputs=self.outputs(context, values),
                                                   content_type=APPLICATION_JSON,
                                                   metadata={self.kind: {"watch": stats}}, logs=logs))

        listener.ChangeWatcher(config, resolve_secret=resolve, storage_dir=services.get("get_block_storage_dir"),
                               emit=emit, stop=context.stop_requested).run()

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Apply validates settings only; it never contacts HubSpot or reads the wallet."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    detail = str(exc) if isinstance(exc, logic.HubSpotError) else logic.MESSAGES["config"]
                    return {"error": self.translate("block.hubspot.error", {"detail": detail},
                                                    fallback="HubSpot settings rejected: " + detail)}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the access mode, object and default operation, never the wallet reference."""
        config = {**self.default_config(), **(node.get("config") or {})}
        operation = config.get("operation") if config.get("operation") in logic.OPERATIONS else "search"
        kind = config.get("object_type") if config.get("object_type") in logic.OBJECT_TYPES else "contacts"
        params = {"operation": operation, "object_type": kind}
        if config.get("enabled") is not True:
            key, params, fallback = "block.hubspot.card_disabled", {}, "Remote access off"
        elif config.get("watch") is True:
            key, fallback = "block.hubspot.card_watch", f"Watching {kind} · {operation}"
        elif config.get("allow_writes") is not True:
            key, fallback = "block.hubspot.card_read", f"Read only · {kind} · {operation}"
        elif config.get("allow_delete") is True:
            key, fallback = "block.hubspot.card_delete", f"Read, write and archive · {kind} · {operation}"
        else:
            key, fallback = "block.hubspot.card_write", f"Read and write · {kind} · {operation}"
        return ui.card(self, node, key, params, self.translate(key, params, fallback=fallback))

"""Package-owned forms, sharing only the framework's public binding helpers."""

from html import escape
import json
from bloxsmith_app.block_api import render_inspector_template, render_node_card_template

from . import logic


def replace_config(result, node):
    """Replace edited JSON settings exactly; generic graph patches otherwise deep-merge objects."""
    patch = result.get("node_patch") if isinstance(result, dict) else None
    if not isinstance(patch, dict) or not isinstance(patch.get("config"), dict):
        return result
    settings = {**(node.get("config") or {}), **patch.pop("config")}
    result.setdefault("graph_operations", []).append({"op": "update_node_config",
        "node_id": str(node.get("id") or ""), "config": settings, "replace": True})
    return result


def suggestions(ident, kind):
    """Standard property names of the selected object; the settings script swaps them when the object changes.

    The catalogue of every object travels in a data attribute, so no request reaches HubSpot from the form.
    """
    names = logic.SUGGESTED_PROPERTIES.get(kind, logic.SUGGESTED_PROPERTIES["contacts"])
    options = "".join(f'<option value="{escape(name, quote=True)}"></option>' for name in names)
    catalogue = escape(json.dumps(logic.SUGGESTED_PROPERTIES, separators=(",", ":")), quote=True)
    return f'<datalist id="{ident}" data-hubspot-property-names="{catalogue}">{options}</datalist>'


def fields(block, node, surface):
    """Render trusted field definitions with escaped values, labels and translated help."""
    specs = json.loads((block.directory / "fields.json").read_text(encoding="utf-8"))
    config = {**block.default_config(), **(node.get("config") or {})}
    groups = {"main": [], "advanced": []}
    prefix = escape(f"{block.kind}-{node.get('id', '')}-{surface}", quote=True)
    shown = {"write": config.get("allow_writes") is True, "watch": config.get("watch") is True}
    kind = config.get("object_type") if config.get("object_type") in logic.OBJECT_TYPES else "contacts"
    for spec in specs:
        key, kind_of = spec["key"], spec.get("type", "text")
        value = config.get(key, "")
        ident = f"{prefix}-{key}"
        attrs = f'data-block-config-field="{key}"'
        if kind_of in {"json", "integer", "boolean"}:
            attrs += f' data-block-value-type="{kind_of}"'
        label_key = f"block.{block.kind}.label_{key}"
        label = block.translate(label_key, fallback=spec["label"])
        hint_key = f"block.{block.kind}.help_{key}"
        hint = escape(block.translate(hint_key, fallback=spec["help"]))
        help_html = f'<p id="{ident}-hint" class="field-hint" data-i18n="{hint_key}">{hint}</p>'
        common = f'id="{ident}" {attrs} aria-describedby="{ident}-hint"'
        extra = ""
        if kind_of == "select":
            options = "".join(f'<option value="{escape(option, quote=True)}"{" selected" if option == value else ""}>{escape(option)}</option>'
                              for option in spec["options"])
            control = f'<select {common}>{options}</select>'
        elif kind_of == "json":
            text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            control = f'<textarea {common} rows="{spec.get("rows", 3)}" spellcheck="false">{escape(text)}</textarea>'
        elif kind_of == "boolean":
            control = f'<input {common} type="checkbox"{" checked" if value is True else ""}>'
        elif kind_of == "integer":
            control = (f'<input {common} type="number" min="{spec["min"]}" max="{spec["max"]}" step="1" '
                       f'value="{escape(str(value), quote=True)}">')
        else:
            placeholder = escape(spec.get("placeholder", ""), quote=True)
            if spec.get("suggest"):
                common += f' list="{ident}-names"'
                extra = suggestions(f"{ident}-names", kind)
            control = (f'<input {common} type="text" autocomplete="off" spellcheck="false" '
                       f'placeholder="{placeholder}" value="{escape(str(value if value is not None else ""), quote=True)}">')
        label_html = f'<label for="{ident}" data-i18n="{label_key}">{escape(label)}</label>'
        rendered = f'<div class="field-group">{label_html}{control}{extra}{help_html}</div>'
        group = spec.get("group")
        if group in shown:
            hidden = "" if shown[group] else " hidden"
            rendered = f'<div class="owned-conditional" data-hubspot-show="{group}"{hidden}>{rendered}</div>'
        groups["advanced" if spec.get("advanced") else "main"].append(rendered)
    main = '<div class="owned-fields">' + "".join(groups["main"]) + "</div>"
    if groups["advanced"]:
        key = f"block.{block.kind}.advanced"
        main += (f'<details class="owned-advanced"><summary data-i18n="{key}">{escape(block.translate(key, fallback="Advanced settings"))}</summary>'
                 f'<div class="owned-fields">{"".join(groups["advanced"])}</div></details>')
    return main


def modal(block, node, payload):
    """Use the generic draft host; refreshing runtime must not replace pending settings."""
    template = (block.directory / "block_modal.html").read_text(encoding="utf-8")
    html = block._render_generic_modal_template(template=template, node=node, payload=payload or {})
    html = html.replace("{{ settings }}", fields(block, node, "modal"))
    return {"html": html, "context": {"node_id": str(node.get("id", "")), "node_kind": block.kind}}


def inspector(block, node, payload):
    """Render the same settings with the standard Ports tab and sticky Apply control."""
    template = (block.directory / "inspector_panel.html").read_text(encoding="utf-8")
    html = render_inspector_template(template=template, node={**node, "kind": block.kind, "type": block.kind},
                                    payload=payload, replacements={"settings": fields(block, node, "inspector")})
    return {"html": html, "context": {"node_id": str(node.get("id", "")), "full_panel": True}}


def card(block, node, preview_key, preview_params, preview):
    """Keep name, purpose and access mode inside the existing card hierarchy.

    The preview carries its catalog key and parameters (`data-i18n`, `data-i18n-params`)
    so the editor shows it in the active language; the template escapes every value.
    """
    return render_node_card_template(block=block, node=node, node_classes=[block.kind + "-node"],
           replacements={"title": node.get("title") or block.default_title(), "preview": preview,
                         "preview_key": preview_key,
                         "preview_params": json.dumps(preview_params) if preview_params else "",
                         "purpose": block.description()})

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCD → Harness NG converter (clean YAML + reusable templates)

- Python 3.9+ compatible (no 3.10-style unions).
- Produces Harness-compliant YAML (indentation, booleans, structure).
- Sanitizes all identifiers to [A-Za-z0-9_]{1,128}.
- Injects orgIdentifier / projectIdentifier into top-level entities.
- Matches reusable StepGroup templates via .harness/template-registry.yaml.
- Re-parses written YAML for quick validation.

Usage:
  python Scripts/ucd_to_harness.py \
    --input raw_files/ucd-export.json \
    --out harness_out \
    --org my_org --project my_project \
    [--registry .harness/template-registry.yaml] [--first-match]

Output:
  harness_out/.harness/services/*.yaml
  harness_out/.harness/pipelines/*.yaml
"""

import os
import re
import sys
import json
import yaml
import argparse
from typing import Any, Dict, List, Optional, Tuple

# -----------------------
# Identifier sanitation
# -----------------------
_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAN = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_identifier(s: str) -> str:
    s = (s or "id").strip()
    s = _SAN.sub("_", s)
    if not s or not re.match(r"^[A-Za-z_]", s):
        s = f"_{s}" if s else "_id"
    s = re.sub(r"_+", "_", s).strip("_")
    return (s or "id")[:128]


def _walk_fix_ids(obj: Any) -> None:
    """Recursively sanitize 'identifier' fields inside nested structures."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "identifier" and isinstance(v, str) and not _ID_RE.match(v):
                obj[k] = sanitize_identifier(v)
            _walk_fix_ids(v)
    elif isinstance(obj, list):
        for i in obj:
            _walk_fix_ids(i)


# -----------------------
# YAML IO helpers
# -----------------------
def _ensure_meta(payload: Dict[str, Any], kind: str, org: str, proj: str) -> None:
    node = payload.get(kind)
    if not isinstance(node, dict):
        return
    node.setdefault("orgIdentifier", org)
    node.setdefault("projectIdentifier", proj)
    if "identifier" in node:
        if not isinstance(node["identifier"], str) or not _ID_RE.match(node["identifier"]):
            node["identifier"] = sanitize_identifier(str(node["identifier"]))
    elif "name" in node:
        node["identifier"] = sanitize_identifier(str(node["name"]))


def write_yaml(path: str, payload: Dict[str, Any], org: str, proj: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Add meta to known tops
    for top in ("pipeline", "service", "template", "environment", "infrastructureDefinition"):
        if top in payload:
            _ensure_meta(payload, top, org, proj)
    # Recursive identifier cleanup
    _walk_fix_ids(payload)
    # Dump + validate
    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    # Parse back to catch bad YAML early
    with open(path, "r", encoding="utf-8") as f:
        yaml.safe_load(f)


# -----------------------
# UCD parsing helpers
# -----------------------
def load_ucd_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_tag(name: str) -> Tuple[str, str]:
    """Convert 'key:value' to (key, value); bare tag → (tag, 'true')."""
    name = (name or "").strip()
    if ":" in name:
        k, v = name.split(":", 1)
        return k.strip(), v.strip()
    return name, "true"


def collect_tags_map(tag_objs: List[Dict[str, Any]]) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for t in tag_objs or []:
        raw = t.get("name") if isinstance(t, dict) else str(t)
        k, v = _parse_tag(str(raw))
        if k:
            tags[k] = v
    return tags


def collect_tags_flat(tag_objs: List[Dict[str, Any]]) -> List[str]:
    flat: List[str] = []
    for t in tag_objs or []:
        raw = t.get("name") if isinstance(t, dict) else str(t)
        flat.append(str(raw))
    return flat


# -----------------------
# Registry (templates) matching
# -----------------------
def load_registry(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path:
        return []
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    items = data.get("templates") or []
    # Normalize structure
    normalized: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        match = it.get("match") or {}
        normalized.append({
            "name": it.get("name") or it.get("templateRef") or "StepGroup",
            "templateRef": it.get("templateRef"),
            "versionLabel": it.get("versionLabel", "v1"),
            "type": it.get("type", "StepGroup"),
            "match": {
                "tags_any": match.get("tags_any") or [],
                "tags_all": match.get("tags_all") or [],
                "any_regex": match.get("any_regex") or [],
                "all_regex": match.get("all_regex") or [],
            },
            "inputs": it.get("inputs") or {},
        })
    return normalized


def _regex_match_any(patterns: List[str], hay: str) -> bool:
    for p in patterns:
        try:
            if re.search(p, hay, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _regex_match_all(patterns: List[str], hay: str) -> bool:
    for p in patterns:
        try:
            if not re.search(p, hay, re.IGNORECASE):
                return False
        except re.error:
            return False
    return True


def _build_template_inputs(inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Transform registry inputs into Harness templateInputs structure."""
    if not inputs:
        return None
    # Currently we support step-group level variables
    vars_map = inputs.get("variables") or {}
    if not vars_map:
        return None
    vars_list = []
    for k, v in vars_map.items():
        vars_list.append({"name": str(k), "type": "String", "value": str(v)})
    return {"variables": vars_list}


def match_stepgroups_for_component(app_name: str,
                                   comp_name: str,
                                   app_tags: List[str],
                                   comp_tags: List[str],
                                   registry: List[Dict[str, Any]],
                                   first_match: bool = False) -> List[Dict[str, Any]]:
    tags_set = set([t.lower() for t in app_tags + comp_tags])
    hay = " ".join([app_name, comp_name] + app_tags + comp_tags)

    matched: List[Dict[str, Any]] = []
    for rule in registry:
        m = rule.get("match", {})
        tags_any = [t.lower() for t in m.get("tags_any", [])]
        tags_all = [t.lower() for t in m.get("tags_all", [])]
        any_regex = m.get("any_regex", [])
        all_regex = m.get("all_regex", [])

        ok = True
        if tags_any:
            ok = ok and (len(tags_set.intersection(tags_any)) > 0)
        if ok and tags_all:
            ok = ok and all(t in tags_set for t in tags_all)
        if ok and any_regex:
            ok = ok and _regex_match_any(any_regex, hay)
        if ok and all_regex:
            ok = ok and _regex_match_all(all_regex, hay)

        if ok:
            spec = {
                "name": rule["name"],
                "templateRef": rule["templateRef"],
                "versionLabel": rule["versionLabel"],
            }
            ti = _build_template_inputs(rule.get("inputs") or {})
            if ti:
                spec["templateInputs"] = ti
            matched.append(spec)
            if first_match:
                break

    return matched


# -----------------------
# DeploymentType heuristic
# -----------------------
def infer_deployment_type(app_tags: List[str], comp_tags: List[str]) -> str:
    all_tags = " ".join(app_tags + comp_tags).lower()
    # Simple heuristics: TAS vs WinRm vs Custom
    if any(x in all_tags for x in [" tas", "pcf", "tanzu", "cloud foundry"]):
        return "TAS"
    if any(x in all_tags for x in ["iis", "windows_service", "windows service", "msi_deploy", "windows web-content"]):
        return "WinRm"
    return "Custom"


# -----------------------
# Builders
# -----------------------
def build_service_payload(name: str, identifier: str, tags_map: Dict[str, str]) -> Dict[str, Any]:
    return {
        "service": {
            "name": name,
            "identifier": sanitize_identifier(identifier),
            "tags": tags_map or {},
            "serviceDefinition": {
                "type": "Custom",
                "spec": {"variables": []}
            }
        }
    }


def build_stage_for_component(
    svc_identifier: str,
    stage_name: str,
    deployment_type: str,
    matched_stepgroups: List[Dict[str, Any]],
) -> Dict[str, Any]:
    stage = {
        "stage": {
            "name": stage_name,
            "identifier": sanitize_identifier(stage_name),
            "type": "Deployment",
            "spec": {
                "deploymentType": deployment_type,
                "service": {"serviceRef": svc_identifier},
                "environment": {
                    "environmentRef": "<+input>",
                    "deployToAll": True,
                    "infrastructureDefinitions": [{"identifier": "<+input>"}],
                },
                "execution": {"steps": []},
            },
        }
    }
    steps = stage["stage"]["spec"]["execution"]["steps"]

    # Inject StepGroup template calls
    for sg in matched_stepgroups:
        sg_name = sg["name"]
        sg_block = {
            "stepGroup": {
                "name": sg_name,
                "identifier": sanitize_identifier(sg_name),
                "template": {
                    "templateRef": sg["templateRef"],
                    "versionLabel": sg.get("versionLabel", "v1"),
                },
            }
        }
        if "templateInputs" in sg:
            sg_block["stepGroup"]["template"]["templateInputs"] = sg["templateInputs"]
        steps.append(sg_block)

    # Safe placeholder (you can remove later)
    steps.append({
        "step": {
            "name": "Deploy",
            "identifier": "Deploy",
            "type": "ShellScript",
            "spec": {
                "shell": "Bash",
                "onDelegate": True,
                "source": {"type": "Inline", "spec": {"script": "echo TODO: implement deployment"}}
            }
        }
    })
    return stage


def build_pipeline_payload(pipeline_name: str,
                           pipeline_id: str,
                           org: str,
                           proj: str,
                           stages: List[Dict[str, Any]],
                           pipeline_tags: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "pipeline": {
            "name": pipeline_name,
            "identifier": sanitize_identifier(pipeline_id),
            "orgIdentifier": org,
            "projectIdentifier": proj,
            "tags": pipeline_tags or {},
            "stages": stages
        }
    }


# -----------------------
# Main
# -----------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Convert UCD export JSON to Harness YAML")
    parser.add_argument("--input", required=True, help="Path to UCD export JSON")
    parser.add_argument("--out", required=True, help="Output folder (root for .harness)")
    parser.add_argument("--org", required=True, help="Harness orgIdentifier")
    parser.add_argument("--project", required=True, help="Harness projectIdentifier")
    parser.add_argument("--registry", default=".harness/template-registry.yaml",
                        help="Path to template registry YAML (optional)")
    parser.add_argument("--first-match", action="store_true",
                        help="If set, stop after the first matching template rule per component")
    args = parser.parse_args()

    out_root = os.path.join(args.out, ".harness")
    services_dir = os.path.join(out_root, "services")
    pipelines_dir = os.path.join(out_root, "pipelines")
    os.makedirs(services_dir, exist_ok=True)
    os.makedirs(pipelines_dir, exist_ok=True)

    # Load inputs
    ucd = load_ucd_json(args.input)
    registry = load_registry(args.registry)

    total_apps = 0
    for app in (ucd.get("applications") or []):
        app_meta = app.get("application") or {}
        app_name = app_meta.get("name") or "Application"
        app_tags_flat = collect_tags_flat(app_meta.get("tags") or [])
        app_tags_map = collect_tags_map(app_meta.get("tags") or [])

        stages: List[Dict[str, Any]] = []
        service_count = 0

        # Components → Services + Stages
        for comp in (app.get("components") or []):
            comp_name = comp.get("name") or "Component"
            comp_id = comp.get("id") or comp_name
            comp_tags_flat = collect_tags_flat(comp.get("tags") or [])
            comp_tags_map = collect_tags_map(comp.get("tags") or [])

            # Build & write Service
            svc_identifier = sanitize_identifier(comp_name)
            svc_yaml = build_service_payload(comp_name, svc_identifier, {**app_tags_map, **comp_tags_map})
            svc_path = os.path.join(services_dir, f"{svc_identifier}.yaml")
            write_yaml(svc_path, svc_yaml, args.org, args.project)
            service_count += 1

            # Match templates
            matched = match_stepgroups_for_component(
                app_name, comp_name, app_tags_flat, comp_tags_flat, registry, first_match=args.first_match
            )

            # DeploymentType heuristic
            deployment_type = infer_deployment_type(app_tags_flat, comp_tags_flat)

            # Build stage
            stage_name = f"Deploy {comp_name}"
            stage = build_stage_for_component(svc_identifier, stage_name, deployment_type, matched)
            stages.append(stage)

        # Build & write Pipeline (one per application)
        pipeline_name = f"{app_name} deploy"
        pipeline_id = f"{app_name}_deploy"
        pipeline_yaml = build_pipeline_payload(pipeline_name, pipeline_id, args.org, args.project, stages, app_tags_map)
        pipe_path = os.path.join(pipelines_dir, f"{sanitize_identifier(pipeline_id)}.yaml")
        write_yaml(pipe_path, pipeline_yaml, args.org, args.project)

        total_apps += 1
        print(f"Converted application: {app_name}  ->  {service_count} services, 1 pipeline")

    print(f"\nDone. Output written under: {out_root}")


if __name__ == "__main__":
    main()

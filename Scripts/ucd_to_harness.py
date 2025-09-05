#!/usr/bin/env python3
"""
Convert IBM UrbanCode Deploy (UCD) JSON export to Harness YAML resources,
auto-detecting technologies and injecting Harness Step Group templates.

Outputs:
  out/.harness/services/*.yaml
  out/.harness/pipelines/*_deploy.yaml

Uses .harness/template-registry.yaml to map UCD names/tags -> Harness templates.
Falls back to a built-in Gradle rule if the registry is absent.

Usage:
  python Scripts/ucd_to_harness.py \
    --input raw_files/ucd-0822.json \
    --out harness_out \
    --org my_org --project my_project \
    [--registry .harness/template-registry.yaml]

Requires:
  python -m pip install pyyaml
"""

import argparse
import json
import os
import re
from typing import Dict, List, Tuple, Any, Optional

try:
    import yaml
except ImportError:
    raise SystemExit("Missing dependency: PyYAML. Install with: python -m pip install pyyaml")

ID_MAX_LEN = 128

# --------- utils ---------

def sanitize_identifier(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", (name or "id").strip())
    if not re.match(r"[A-Za-z_]", s):
        s = "_" + s
    return s[:ID_MAX_LEN]

def split_tag(tag_name: str) -> Tuple[str, str]:
    if ":" in (tag_name or ""):
        k, v = tag_name.split(":", 1)
        return (k.strip() or "tag", (v.strip() or "true"))
    return ((tag_name or "").strip(), "true")

def ucd_tags_to_dict(tags: List[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in tags or []:
        n = (t.get("name") or "").strip()
        if not n:
            continue
        k, v = split_tag(n)
        out[k] = v
    return out

def detect_deployment_type(app_name: str, component_names: List[str], all_tags: Dict[str, str]) -> str:
    hay = " ".join([app_name] + component_names + list(all_tags.keys()) + list(all_tags.values()))
    hl = hay.lower()
    if any(m in hl for m in ["windows", "iis", "msi", "com", "dcom", "app pool", "app_pool"]):
        return "WinRm"
    if any(m in hl for m in ["pcf", "tanzu", "cloud foundry", "tas"]):
        return "TAS"
    if "informatica" in hl:
        return "Ssh"
    return "Ssh"

def ensure_dirs(base_out: str) -> Tuple[str, str]:
    svc_dir = os.path.join(base_out, ".harness", "services")
    pipe_dir = os.path.join(base_out, ".harness", "pipelines")
    os.makedirs(svc_dir, exist_ok=True)
    os.makedirs(pipe_dir, exist_ok=True)
    return svc_dir, pipe_dir

def safe_dump_yaml(obj: Dict[str, Any]) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)

# --------- registry (auto-detect rules) ---------

def default_registry() -> Dict[str, Any]:
    """Fallback Java/Gradle template if no registry file is present."""
    return {
        "templates": [
            {
                "name": "Java Gradle Build",
                "templateRef": "Java_Gradle_Build",
                "versionLabel": "v1",
                "type": "StepGroup",
                "match": {"any_regex": [r"\bgradle\b", r"\bjava\b", r"\.jar\b", r"\.war\b"]},
                "inputs": {"variables": {"workingDir": ".", "gradleTasks": "clean build", "extraArgs": "", "javaHome": ""}}
            }
        ]
    }

def load_registry(path: Optional[str]) -> Dict[str, Any]:
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    default_path = os.path.join(".harness", "template-registry.yaml")
    if os.path.isfile(default_path):
        with open(default_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return default_registry()

def match_rule(rule: Dict[str, Any], hay_text: str, tags: Dict[str, str]) -> bool:
    m = rule.get("match") or {}
    any_regex = m.get("any_regex") or []
    all_regex = m.get("all_regex") or []
    tags_any = m.get("tags_any") or []  # strings like "key:value" or "key" or just "value"
    try:
        if any_regex and not any(re.search(rx, hay_text, flags=re.IGNORECASE) for rx in any_regex):
            return False
        if all_regex and not all(re.search(rx, hay_text, flags=re.IGNORECASE) for rx in all_regex):
            return False
    except re.error:
        return False
    if tags_any:
        flat = {("%s:%s" % (k, v)).lower() for k, v in tags.items()}
        flat_keys = {k.lower() for k in tags.keys()}
        flat_vals = {v.lower() for v in tags.values()}
        ok = False
        for tok in tags_any:
            t = (tok or "").lower()
            if not t:
                continue
            if ":" in t:
                ok = ok or (t in flat)
            else:
                ok = ok or (t in flat_keys) or (t in flat_vals)
        if not ok:
            return False
    return True

def build_stepgroup_from_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    tref = rule.get("templateRef")
    vlabel = rule.get("versionLabel") or "v1"
    if not tref:
        return {}
    # convert inputs dict -> templateInputs.variables list
    inputs = rule.get("inputs", {}).get("variables", {})
    var_list = [{"name": str(k), "type": "String", "value": "" if v is None else v} for k, v in inputs.items()]
    sg = {
        "stepGroup": {
            "name": rule.get("name", tref),
            "identifier": sanitize_identifier((rule.get("name", tref) or "") + "_Invocation"),
            "template": {"templateRef": tref, "versionLabel": vlabel}
        }
    }
    if var_list:
        sg["stepGroup"]["template"]["templateInputs"] = {"variables": var_list}
    return sg

# --------- builders ---------

def build_service_yaml(name: str, org: str, project: str, tags: Dict[str, str], svc_type: str) -> Dict[str, Any]:
    return {
        "service": {
            "name": name,
            "identifier": sanitize_identifier(name),
            "orgIdentifier": org,
            "projectIdentifier": project,
            "tags": tags or {},
            "serviceDefinition": {"type": svc_type, "spec": {}}
        }
    }

def build_pipeline_yaml(app_name: str, org: str, project: str,
                        app_tags: Dict[str, str], stages: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "pipeline": {
            "name": "%s - deploy" % app_name,
            "identifier": sanitize_identifier("%s_deploy" % app_name),
            "orgIdentifier": org,
            "projectIdentifier": project,
            "tags": app_tags or {},
            "stages": stages
        }
    }

def build_stage(comp_name: str, svc_identifier: str, deploy_type: str,
                injected_stepgroups: List[Dict[str, Any]]) -> Dict[str, Any]:
    shell = "PowerShell" if deploy_type == "WinRm" else "Bash"
    steps: List[Dict[str, Any]] = []
    for sg in injected_stepgroups:
        if sg:
            steps.append(sg)
    steps.append({
        "step": {
            "name": "Deploy",
            "identifier": "Deploy",
            "type": "ShellScript",
            "spec": {
                "shell": shell,
                "onDelegate": True,
                "source": {"type": "Inline", "spec": {"script": (
                    'Write-Host "TODO: implement deployment"' if shell == "PowerShell"
                    else 'echo "TODO: implement deployment"'
                )}}
            }
        }
    })
    return {
        "stage": {
            "name": comp_name,
            "identifier": sanitize_identifier(comp_name),
            "type": "Deployment",
            "spec": {
                "deploymentType": deploy_type,
                "service": {"serviceRef": svc_identifier},
                "environment": {
                    "environmentRef": "<+input>",
                    "infrastructureDefinitions": [{"identifier": "<+input>"}]
                },
                "execution": {"steps": steps}
            }
        }
    }

# --------- main conversion ---------

def convert_ucd_to_harness(ucd: Dict[str, Any], out_dir: str, org: str, project: str, registry_path: Optional[str]) -> None:
    svc_dir, pipe_dir = ensure_dirs(out_dir)
    registry = load_registry(registry_path)
    rules = registry.get("templates") or []

    apps = ucd.get("applications") or []
    if not apps:
        print("No applications found in UCD JSON.")
        return

    for app_entry in apps:
        app = app_entry.get("application", {}) or {}
        app_name = app.get("name", "Application")
        app_tags = ucd_tags_to_dict(app.get("tags", []))

        components = app_entry.get("components", []) or []
        comp_names = [c.get("name", "") for c in components]

        # aggregate for type detection
        comp_tags_agg: Dict[str, str] = {}
        for c in components:
            comp_tags_agg.update(ucd_tags_to_dict(c.get("tags", [])))

        deploy_type = detect_deployment_type(app_name, comp_names, dict(list(app_tags.items()) + list(comp_tags_agg.items())))
        stages: List[Dict[str, Any]] = []

        for comp in components:
            comp_name = comp.get("name", "Component")
            comp_tags = ucd_tags_to_dict(comp.get("tags", []))

            # write Service
            svc_yaml = build_service_yaml(comp_name, org, project, comp_tags, deploy_type)
            svc_identifier = svc_yaml["service"]["identifier"]
            with open(os.path.join(svc_dir, "%s.yaml" % svc_identifier), "w", encoding="utf-8") as f:
                f.write(safe_dump_yaml(svc_yaml))

            # detection haystack (names + tags)
            hay = " ".join([
                app_name,
                comp_name,
                " ".join(comp_names),
                " ".join(["%s:%s" % (k, v) for k, v in dict(list(app_tags.items()) + list(comp_tags.items())).items()])
            ])

            # rules -> step groups
            matched_stepgroups: List[Dict[str, Any]] = []
            for rule in rules:
                if match_rule(rule, hay, dict(list(app_tags.items()) + list(comp_tags.items()))):
                    sg = build_stepgroup_from_rule(rule)
                    if sg:
                        matched_stepgroups.append(sg)

            # stage
            stages.append(build_stage(comp_name, svc_identifier, deploy_type, matched_stepgroups))

        if stages:
            pipe_yaml = build_pipeline_yaml(app_name, org, project, app_tags, stages)
            pipe_identifier = pipe_yaml["pipeline"]["identifier"]
            with open(os.path.join(pipe_dir, "%s.yaml" % pipe_identifier), "w", encoding="utf-8") as f:
                f.write(safe_dump_yaml(pipe_yaml))

        print("Converted application: %s  ->  %d services, %d pipeline" % (app_name, len(components), 1 if stages else 0))

    print("\nDone. Output written under: %s" % os.path.abspath(out_dir))

def main():
    ap = argparse.ArgumentParser(description="Convert UCD JSON export to Harness YAML with auto template detection.")
    ap.add_argument("--input", "-i", required=True, help="Path to UCD JSON export")
    ap.add_argument("--out", "-o", required=True, help="Output directory")
    ap.add_argument("--org", required=True, help="Harness orgIdentifier")
    ap.add_argument("--project", required=True, help="Harness projectIdentifier")
    ap.add_argument("--registry", help="Optional path to template-registry.yaml (default .harness/template-registry.yaml)")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        ucd = json.load(f)

    convert_ucd_to_harness(ucd, args.out, args.org, args.project, args.registry)

if __name__ == "__main__":
    main()

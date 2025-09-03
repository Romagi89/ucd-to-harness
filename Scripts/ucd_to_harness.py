#!/usr/bin/env python3
"""
Convert IBM UrbanCode Deploy (UCD) JSON export to Harness YAML resources.

Outputs:
  out_dir/
    .harness/
      services/<service>.yaml             # one per UCD component
      pipelines/<app>_deploy.yaml         # one per UCD application

Features:
- Infers deployment type per application: WinRm (Windows/IIS/MSI/COM), TAS (PCF/CF/TAS), or Ssh (default/Informatica).
- Maps UCD tags -> Harness tags.
- Creates one Deployment stage per component.
- Optionally injects a reusable Step Group template (Java/Gradle library) into matched stages.

Usage:
  python ucd_to_harness.py \
    --input ucd.json \
    --out harness_out \
    --org my_org --project my_project \
    --gradle-template-ref Java_Gradle_Build \
    --gradle-template-version v1 \
    --gradle-match "java|gradle|jar|war"

Requires:
  pip install pyyaml
"""

import argparse
import json
import os
import re
from typing import Dict, List, Tuple, Any

try:
    import yaml
except ImportError:
    raise SystemExit("Missing dependency: PyYAML. Install with: python -m pip install pyyaml")

ID_MAX_LEN = 128


# --------------------------
# Helpers
# --------------------------

def sanitize_identifier(name: str) -> str:
    """Convert an arbitrary name to a Harness-safe identifier (A-Za-z0-9_)."""
    if not name:
        return "id"
    s = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if not re.match(r"[A-Za-z_]", s):
        s = "_" + s
    return s[:ID_MAX_LEN]


def split_tag(tag_name: str) -> Tuple[str, str]:
    """Convert 'key:value' style tags to (key, value). If no ':', return (name, "true")."""
    if ":" in tag_name:
        key, val = tag_name.split(":", 1)
        key = key.strip() or "tag"
        val = val.strip() or "true"
        return key, val
    return (tag_name.strip(), "true")


def ucd_tags_to_harness(tags: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map UCD tag array (with 'name') to Harness tags dict."""
    out: Dict[str, str] = {}
    for t in tags or []:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        k, v = split_tag(name)
        out[k] = v
    return out


def detect_deployment_type(app_name: str, component_names: List[str], all_tags: Dict[str, str]) -> str:
    """
    Heuristic to pick Harness deployment type: 'WinRm', 'TAS', or 'Ssh'.
    """
    hay = " ".join([app_name] + component_names + list(all_tags.keys()) + list(all_tags.values()))
    hl = hay.lower()

    # Windows/IIS markers
    if any(m in hl for m in ["windows", "iis", "msi", "com", "dcom", "app pool", "app_pool"]):
        return "WinRm"
    # PCF / TAS markers
    if any(m in hl for m in ["pcf", "tanzu", "cloud foundry", "tas"]):
        return "TAS"
    # Informatica marker -> Ssh by default
    if "informatica" in hl:
        return "Ssh"
    # Default
    return "Ssh"


def looks_like_gradle(java_text: str, match_regex: str) -> bool:
    """Return True if the text matches the Gradle/Java regex."""
    try:
        return re.search(match_regex, java_text, flags=re.IGNORECASE) is not None
    except re.error:
        # bad regex -> disable matching
        return False


def ensure_dirs(base_out: str) -> Tuple[str, str]:
    services_dir = os.path.join(base_out, ".harness", "services")
    pipelines_dir = os.path.join(base_out, ".harness", "pipelines")
    os.makedirs(services_dir, exist_ok=True)
    os.makedirs(pipelines_dir, exist_ok=True)
    return services_dir, pipelines_dir


def safe_dump_yaml(obj: Dict[str, Any]) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)


# --------------------------
# Builders
# --------------------------

def build_service_yaml(name: str, org: str, project: str, tags: Dict[str, str], svc_type: str) -> Dict[str, Any]:
    """Build a minimal Harness Service YAML dict."""
    return {
        "service": {
            "name": name,
            "identifier": sanitize_identifier(name),
            "orgIdentifier": org,
            "projectIdentifier": project,
            "tags": tags or {},
            "serviceDefinition": {
                "type": svc_type,   # "WinRm" | "TAS" | "Ssh" | etc.
                "spec": {}
            }
        }
    }


def build_pipeline_yaml(app_name: str,
                        org: str,
                        project: str,
                        app_tags: Dict[str, str],
                        stages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a Harness Pipeline YAML dict with provided stages."""
    return {
        "pipeline": {
            "name": f"{app_name} - deploy",
            "identifier": sanitize_identifier(f"{app_name}_deploy"),
            "orgIdentifier": org,
            "projectIdentifier": project,
            "tags": app_tags or {},
            "stages": stages
        }
    }


def build_stage_for_component(comp_name: str,
                              svc_identifier: str,
                              deploy_type: str,
                              gradle_template_ref: str = None,
                              gradle_template_version: str = "v1",
                              gradle_match_regex: str = r"java|gradle|jar|war",
                              comp_tags_text: str = "") -> Dict[str, Any]:
    """
    Build a generic Deployment stage that targets the given service.
    Environment/infra are runtime to let user choose at run time.
    Optionally inject a Step Group template for Java/Gradle.
    """
    shell = "PowerShell" if deploy_type == "WinRm" else "Bash"
    step_script = (
        f'Write-Host "TODO: implement deployment for component: {comp_name}"'
        if deploy_type == "WinRm"
        else f'echo "TODO: implement deployment for component: {comp_name}"'
    )

    # Decide whether to inject the Gradle template
    hay = f"{comp_name} {comp_tags_text}"
    add_gradle = bool(gradle_template_ref) and looks_like_gradle(hay, gradle_match_regex)

    steps: List[Dict[str, Any]] = []

    if add_gradle:
        steps.append({
            "stepGroup": {
                "name": "Java/Gradle Build",
                "identifier": "Java_Gradle_Build_Invocation",
                "template": {
                    "templateRef": gradle_template_ref,
                    "versionLabel": gradle_template_version,
                    # Default inputs; adjust as desired or remove to make all runtime
                    "templateInputs": {
                        "variables": [
                            {"name": "workingDir",  "type": "String", "value": "."},
                            {"name": "gradleTasks", "type": "String", "value": "clean build"},
                            {"name": "extraArgs",   "type": "String", "value": ""},
                            {"name": "javaHome",    "type": "String", "value": ""}
                        ]
                    }
                }
            }
        })

    # Always include a Deploy placeholder step
    steps.append({
        "step": {
            "name": "Deploy",
            "identifier": "Deploy",
            "type": "ShellScript",
            "spec": {
                "shell": shell,
                "onDelegate": True,
                "source": {"type": "Inline", "spec": {"script": step_script}}
            }
        }
    })

    return {
        "stage": {
            "name": comp_name,
            "identifier": sanitize_identifier(comp_name),
            "type": "Deployment",
            "spec": {
                "deploymentType": deploy_type,  # must match serviceDefinition.type
                "service": {"serviceRef": svc_identifier},
                "environment": {
                    "environmentRef": "<+input>",  # pick Dev/QA/Prod at runtime
                    "infrastructureDefinitions": [{"identifier": "<+input>"}]  # pick infra at runtime
                },
                "execution": {"steps": steps}
            }
        }
    }


# --------------------------
# Main conversion
# --------------------------

def convert_ucd_to_harness(ucd: Dict[str, Any],
                           out_dir: str,
                           org: str,
                           project: str,
                           gradle_template_ref: str,
                           gradle_template_version: str,
                           gradle_match_regex: str) -> None:
    services_dir, pipelines_dir = ensure_dirs(out_dir)

    apps = ucd.get("applications") or []
    if not apps:
        print("No applications found in UCD JSON.")
        return

    for app_entry in apps:
        app = app_entry.get("application", {})
        app_name = app.get("name", "Application")
        app_tags = ucd_tags_to_harness(app.get("tags", []))

        # Gather component info
        components = app_entry.get("components", []) or []
        comp_names = [c.get("name", "") for c in components]

        # Aggregate tags for type detection
        comp_tags_agg: Dict[str, str] = {}
        for c in components:
            comp_tags_agg.update(ucd_tags_to_harness(c.get("tags", [])))

        deploy_type = detect_deployment_type(app_name, comp_names, {**app_tags, **comp_tags_agg})

        # Build Services for each component + corresponding stages
        stages: List[Dict[str, Any]] = []
        for comp in components:
            comp_name = comp.get("name", "Component")
            comp_tags = ucd_tags_to_harness(comp.get("tags", []))
            comp_tags_text = " ".join([f"{k}:{v}" for k, v in comp_tags.items()])

            svc_yaml = build_service_yaml(comp_name, org, project, comp_tags, deploy_type)
            svc_identifier = svc_yaml["service"]["identifier"]

            svc_filename = os.path.join(services_dir, f"{svc_identifier}.yaml")
            with open(svc_filename, "w", encoding="utf-8") as f:
                f.write(safe_dump_yaml(svc_yaml))

            stages.append(
                build_stage_for_component(
                    comp_name=comp_name,
                    svc_identifier=svc_identifier,
                    deploy_type=deploy_type,
                    gradle_template_ref=gradle_template_ref,
                    gradle_template_version=gradle_template_version,
                    gradle_match_regex=gradle_match_regex,
                    comp_tags_text=comp_tags_text
                )
            )

        # Build a pipeline for the application (if it has components)
        if stages:
            pipe_yaml = build_pipeline_yaml(app_name, org, project, app_tags, stages)
            pipe_identifier = pipe_yaml["pipeline"]["identifier"]
            pipe_filename = os.path.join(pipelines_dir, f"{pipe_identifier}.yaml")
            with open(pipe_filename, "w", encoding="utf-8") as f:
                f.write(safe_dump_yaml(pipe_yaml))

        print(f"Converted application: {app_name}  ->  {len(components)} services, {1 if stages else 0} pipeline")

    print(f"\nDone. Output written under: {os.path.abspath(out_dir)}")


# --------------------------
# CLI
# --------------------------

def main():
    parser = argparse.ArgumentParser(description="Convert UCD JSON export to Harness YAML.")
    parser.add_argument("--input", "-i", required=True, help="Path to UCD JSON export file")
    parser.add_argument("--out", "-o", required=True, help="Output directory")
    parser.add_argument("--org", required=True, help="Harness orgIdentifier")
    parser.add_argument("--project", required=True, help="Harness projectIdentifier")
    parser.add_argument("--gradle-template-ref", default="Java_Gradle_Build",
                        help="Harness StepGroup templateRef for Gradle (default: Java_Gradle_Build)")
    parser.add_argument("--gradle-template-version", default="v1",
                        help="Template versionLabel (default: v1)")
    parser.add_argument("--gradle-match", default=r"java|gradle|jar|war",
                        help="Regex to detect components that should call the Gradle template")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        ucd = json.load(f)

    convert_ucd_to_harness(
        ucd=ucd,
        out_dir=args.out,
        org=args.org,
        project=args.project,
        gradle_template_ref=args.gradle_template_ref,
        gradle_template_version=args.gradle_template_version,
        gradle_match_regex=args.gradle_match
    )


if __name__ == "__main__":
    main()

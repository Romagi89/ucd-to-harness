| Concept               | uDeploy JSON (IBM)                      | Harness YAML (Harness.io)                                |
| --------------------- | --------------------------------------- | -------------------------------------------------------- |
| **Process Name**      | `"name": "Deploy Component"`            | `pipeline: name: WebApp-Deployment`                      |
| **Steps**             | `"componentProcessStep"` objects        | `steps:` under execution                                 |
| **Shell Command**     | `"command": "systemctl stop tomcat"`    | `ShellScript step → script:`                             |
| **Artifact Download** | `"Download Artifacts"` with destination | `DownloadArtifact` with `destinationPath`                |
| **Properties / Vars** | `${p:version/name}` placeholders        | `connectorRef`, `artifactRef`, and environment variables |
| **Tags**              | `"tags": ["deploy","webapp"]`           | `tags: { deploy: "webapp" }`                             |


How to use
pip install pyyaml
python ucd_to_harness.py \
  --input ucd_export.json \
  --out harness_out \
  --org my_org \
  --project my_project


This will generate:

harness_out/.harness/services/*.yaml      # one service per UCD component
harness_out/.harness/pipelines/*_deploy.yaml  # one pipeline per UCD application


Each pipeline has:

one Deployment stage per component

runtime inputs for environmentRef and infrastructureDefinitions[0].identifier (pick Dev/QA/Prod + infra at run)

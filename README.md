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
python Scripts/ucd_to_harness.py \                                         
  --input raw_files/ucd-0822.json \
  --out harness_out \
  --org my_org --project my_project \
  --gradle-template-ref Java_Gradle_Build \
  --gradle-template-version v1 \
  --gradle-match "java|gradle|jar|war"

python Scripts/ucd_to_harness.py \         
  --input raw_files/ucd-demo.json \
  --out harness_out \
  --org my_org --project my_project

  
This will generate:

harness_out/.harness/services/*.yaml      # one service per UCD component
harness_out/.harness/pipelines/*_deploy.yaml  # one pipeline per UCD application


Each pipeline has:

one Deployment stage per component

runtime inputs for environmentRef and infrastructureDefinitions[0].identifier (pick Dev/QA/Prod + infra at run)
========
What happens during conversion (step-by-step)

Read UCD JSON
applications[*].application.name and each components[*].name/tags are parsed.

Create one Harness Service per UCD component

Written to: harness_out/.harness/services/<serviceId>.yaml

Carries the UCD component’s tags (useful for search/governance).

Choose a Harness deploymentType per component

Simple keyword heuristics:

WinRm if it looks Windows/IIS/MSI/COM

TAS if PCF/Tanzu/TAS

SSH for Informatica/Unixy stuff (default fallback)

Match reusable templates via the registry

For each component, the script builds a “haystack” string from:
app name + component name + all component names + app tags + component tags.

It scans every rule in .harness/template-registry.yaml.

tags_any: matches key:value (e.g. Windows_Service), key-only, or value-only tokens from UCD tags

any_regex / all_regex: extra name-based matching

Every rule that matches is added to the stage, in the order they appear in the registry.

Build a Deployment stage per component

Stage YAML includes:

the ServiceRef

environmentRef / infrastructureDefinitions set to <+input> (you pick these at run time)

an execution.steps list that contains each matched StepGroup as a templateRef, plus a final “TODO: implement deployment” placeholder step (you can remove/replace it).

Write the Pipeline (one per application)

File: harness_out/.harness/pipelines/<app>_deploy.yaml

Contains all the stages (one per component) with the injected template calls.

At import/run time in Harness

Harness resolves each stepGroup.template.templateRef to the Template you created in the UI (or via Git), same org/project and matching versionLabel.

If a StepGroup defines variables with value: <+input>, Harness will prompt you (or you can set inputs upstream in the pipeline).

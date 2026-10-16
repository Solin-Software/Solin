"""Issue entry points and trusted PR label automation boundaries."""

import json

import yaml

from tests._paths import REPO_ROOT


def configuration(path):
    return yaml.load((REPO_ROOT / path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_issue_chooser_uses_two_labeled_forms_and_discussions():
    root = ".github/ISSUE_TEMPLATE"
    chooser = configuration(f"{root}/config.yml")
    assert chooser["blank_issues_enabled"] == "false"
    assert chooser["contact_links"] == [{
        "name": "Discussions", "url": "https://github.com/Solin-Software/Solin/discussions",
        "about": "Ask questions, exchange ideas, and discuss Solin with the community.",
    }]
    assert configuration(f"{root}/bug_report.yml")["labels"] == ["bug"]
    assert configuration(f"{root}/feature_request.yml")["labels"] == ["enhancement"]
    assert {path.name for path in (REPO_ROOT / root).glob("*.yml")} == {
        "config.yml", "bug_report.yml", "feature_request.yml",
    }


def test_label_workflow_runs_only_trusted_code_with_data_from_api_and_env():
    workflow = configuration(".github/workflows/pull-request-labels.yml")
    assert workflow["on"] == {"pull_request_target": {
        "branches": ["main"], "types": ["opened", "edited", "synchronize", "reopened"],
    }}
    assert workflow["permissions"] == {
        "contents": "read", "issues": "write", "pull-requests": "write",
    }
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert "github.event.pull_request.number" in workflow["concurrency"]["group"]
    steps = workflow["jobs"]["classify"]["steps"]
    checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha }}", "persist-credentials": "false",
    }
    classify = steps[-1]
    assert classify["run"] == "python scripts/label_pull_request.py"
    assert classify["env"] == {
        "GH_TOKEN": "${{ github.token }}", "PR_NUMBER": "${{ github.event.pull_request.number }}",
    }
    assert all("${{" not in step.get("run", "") for step in steps)


def test_catalog_has_unique_labels_and_explicit_order_for_release_grouping():
    catalog = json.loads((REPO_ROOT / ".github/labels.json").read_text(encoding="utf-8"))
    names = [rule["name"] for rule in catalog]
    assert len(names) == len(set(names))
    assert names == ["bug", "enhancement", "github_actions", "dependencies",
                     "translations", "documentation"]
    assert all(rule["release_heading"] and len(rule["color"]) == 6 for rule in catalog)

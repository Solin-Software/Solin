"""Automatic classification and persistent manual label decisions."""

import json
import subprocess

import pytest

from scripts import label_pull_request as labels

RULES = json.loads(labels.CATALOG.read_text(encoding="utf-8"))
MANAGED = {rule["name"] for rule in RULES}
ROOT = "repos/Solin-Software/Solin"


@pytest.mark.parametrize(("title", "expected"), [
    ("fix: preserve playback", {"bug"}),
    ("bug(player)!: preserve playback", {"bug"}),
    ("feat(remote): add controls", {"enhancement"}),
    ("enhancement: improve controls", {"enhancement"}),
    ("refactor(media): retire the custom scene engine and QtMultimedia", {"enhancement"}),
    ("refactor(media): migrate thumbnail and metadata extraction to bundled FFmpeg", {"enhancement"}),
    ("perf(media)!: accelerate extraction", {"enhancement"}),
    ("docs: explain setup", {"documentation"}),
    ("ci: cache builds", {"github_actions"}),
    ("test: stabilize native transitions", {"testing"}),
    ("test(qt): isolate reparenting input", {"testing"}),
    ("test(qt)!: replace input fixtures", {"testing"}),
    ("deps: update runtime", {"dependencies"}),
    ("chore(deps-dev): update pytest", {"dependencies"}),
    ("chore(release): prepare 26.32.0", {"skip-changelog"}),
    ("chore(release)!: prepare 26.32.0", {"skip-changelog"}),
    ("chore: maintain packaging", set()),
    ("chore(releases): prepare packaging", set()),
    ("fix(release): restore publication", {"bug"}),
    ("feat(release): add validation", {"enhancement"}),
    ("chore(release):", set()),
    ("prefix chore(release): prepare version", set()),
    ("fix(deps): update runtime", {"bug", "dependencies"}),
    ("i18n: update Portuguese", {"translations"}),
    ("l10n: update Portuguese", {"translations"}),
    ("Improve playback", set()),
    ("prefix fix: playback", set()),
    ("fix playback", set()),
    ("fix:", set()),
    ("feat:   ", set()),
    ("test:", set()),
    ("prefix test: stabilize transitions", set()),
    ("feat\nfix: playback", set()),
])
def test_titles_classify_only_an_explicit_conventional_prefix(title, expected):
    assert labels.suggested_labels(title, [], RULES) == expected


@pytest.mark.parametrize(("path", "expected"), [
    ("README.md", {"documentation"}),
    ("docs/releases.md", {"documentation"}),
    ("requirements.txt", {"dependencies"}),
    ("requirements-dev.txt", {"dependencies"}),
    ("requirements-build.txt", {"dependencies"}),
    ("pyproject.toml", {"dependencies"}),
    (".github/workflows/release.yml", {"github_actions"}),
    (".github/workflows/test.yaml", {"github_actions"}),
    (".github/actions/build/action.yml", {"github_actions"}),
    ("src/solin/resources/translations/solin_pt_BR.ts", {"translations"}),
    ("src/solin/resources/translations/solin_pt_BR.qm", set()),
    ("src/solin/resources/translations/locales/pt_BR.json", {"translations"}),
    ("src/solin/resources/remote_control/scripts/client.ts", set()),
    ("src/solin/version.py", set()),
    ("tests/unit/test_example.py", set()),
    ("docs/requirements.md", {"documentation"}),
])
def test_file_rules_target_the_repository_layout(path, expected):
    assert labels.suggested_labels("Free title", [{"filename": path}], RULES) == expected


@pytest.mark.parametrize(("title", "primary"), [
    ("fix: restore startup", "bug"),
    ("refactor(media): consolidate playback", "enhancement"),
    ("perf(media): accelerate extraction", "enhancement"),
])
def test_title_and_files_are_combined_including_renamed_and_deleted_paths(title, primary):
    files = [
        {"filename": "archive/old.txt", "previous_filename": "docs/old.md", "status": "renamed"},
        {"filename": "requirements-dev.txt", "status": "removed"},
        {"filename": ".github/workflows/test.yml", "status": "added"},
    ]
    assert labels.suggested_labels(title, files, RULES) == {
        primary, "documentation", "dependencies", "github_actions",
    }


def event(name, action="labeled", actor=labels.AUTOMATION_LOGIN):
    return {"event": action, "label": {"name": name}, "actor": {"login": actor} if actor else None}


def test_automation_replaces_its_own_obsolete_type_and_preserves_unmanaged_labels():
    assert labels.label_update(
        {"enhancement"}, {"bug", "help wanted"}, [event("bug")], MANAGED,
    ) == labels.LabelUpdate(("enhancement",), ("bug",))


@pytest.mark.parametrize("actor", ["maintainer", "dependabot[bot]", None])
def test_manual_or_external_removal_is_respected_on_every_future_run(actor):
    history = [event("bug"), event("bug", "unlabeled", actor), {"event": "renamed"}]
    assert labels.label_update({"bug"}, set(), history, MANAGED) == labels.LabelUpdate((), ())


def test_manually_added_label_is_preserved_when_the_title_changes():
    assert labels.label_update(
        set(), {"bug"}, [event("bug", actor="maintainer")], MANAGED,
    ) == labels.LabelUpdate((), ())


def test_existing_labels_without_provenance_are_never_removed():
    assert labels.label_update(set(), {"bug"}, [], MANAGED) == labels.LabelUpdate((), ())


def test_manually_readjusted_label_remains_manual_even_after_other_bot_events():
    history = [event("bug"), event("bug", "unlabeled", "maintainer"), event("bug")]
    assert labels.label_update(set(), {"bug"}, history, MANAGED) == labels.LabelUpdate((), ())


def test_matching_automatic_labels_are_idempotent():
    assert labels.label_update({"bug"}, {"bug"}, [event("bug")], MANAGED) == labels.LabelUpdate((), ())


def test_label_matching_and_manual_ownership_are_case_insensitive():
    assert labels.label_update({"bug"}, {"Bug"}, [], MANAGED) == labels.LabelUpdate((), ())
    assert labels.label_update(
        {"bug"}, set(), [event("BUG", "unlabeled", "maintainer")], MANAGED,
    ) == labels.LabelUpdate((), ())


class GitHub:
    def __init__(self):
        self.pull = {"state": "open", "title": "feat: add controls", "labels": [{"name": "bug"}],
                     "changed_files": 1}
        self.files = [{"filename": "README.md"}]
        self.events = [event("bug")]
        self.available = [{"name": rule["name"]} for rule in RULES]
        self.calls = []
        self.failure = None

    def __call__(self, endpoint, *, method="GET", payload=None):
        self.calls.append((endpoint, method, payload))
        if endpoint == self.failure:
            raise RuntimeError("API unavailable")
        if method != "GET":
            current = {label["name"] for label in self.pull["labels"]}
            if endpoint == f"{ROOT}/issues/12/labels" and method == "POST":
                current.update(payload["labels"])
            elif endpoint.startswith(f"{ROOT}/issues/12/labels/") and method == "DELETE":
                current.discard(endpoint.rsplit("/", 1)[1])
            self.pull["labels"] = [{"name": name} for name in sorted(current)]
            return None
        return {
            f"{ROOT}/pulls/12": self.pull,
            f"{ROOT}/pulls/12/files?per_page=100&page=1": self.files,
            f"{ROOT}/issues/12/events?per_page=100&page=1": self.events,
            f"{ROOT}/labels?per_page=100&page=1": self.available,
        }[endpoint]

    @property
    def writes(self):
        return [call for call in self.calls if call[1] != "GET"]


def test_full_reconciliation_adds_without_replacing_manual_labels_and_removes_only_stale_ones():
    api = GitHub()
    update = labels.apply_labels("Solin-Software/Solin", 12, RULES, api)
    assert update == labels.LabelUpdate(("documentation", "enhancement"), ("bug",))
    assert api.writes == [
        (f"{ROOT}/issues/12/labels", "POST", {"labels": ["documentation", "enhancement"]}),
        (f"{ROOT}/issues/12/labels/bug", "DELETE", None),
    ]


def test_selectorless_label_is_never_suggested_or_removed_even_if_added_by_the_bot():
    rules = [{**rule, "title_prefixes": []} for rule in RULES]
    assert "skip-changelog" not in labels.suggested_labels("skip-changelog: hide entry", [], RULES)
    api = GitHub()
    api.pull.update(title="Free title", labels=[{"name": "skip-changelog"}])
    api.files = [{"filename": "src/main.py"}]
    api.events = [event("skip-changelog")]

    assert labels.apply_labels("Solin-Software/Solin", 12, rules, api) == labels.LabelUpdate((), ())
    assert api.pull["labels"] == [{"name": "skip-changelog"}]
    assert api.writes == []


@pytest.mark.parametrize(("title", "current", "history", "expected"), [
    ("chore(release): prepare version", set(), [], labels.LabelUpdate(("skip-changelog",), ())),
    ("fix(release): restore publication", {"skip-changelog"}, [event("skip-changelog")],
     labels.LabelUpdate(("bug",), ("skip-changelog",))),
    ("chore(release): prepare version", set(),
     [event("skip-changelog"), event("skip-changelog", "unlabeled", "maintainer")],
     labels.LabelUpdate((), ())),
    ("chore: maintain packaging", {"skip-changelog"},
     [event("skip-changelog", actor="maintainer")], labels.LabelUpdate((), ())),
])
def test_release_exclusion_follows_title_changes_and_manual_overrides(title, current, history, expected):
    api = GitHub()
    api.pull.update(title=title, labels=[{"name": name} for name in current])
    api.files = [{"filename": "src/main.py"}]
    api.events = history
    assert labels.apply_labels("Solin-Software/Solin", 12, RULES, api) == expected
    assert {label["name"] for label in api.pull["labels"]} == (current | set(expected.add)) - set(expected.remove)


def test_scoped_title_rules_are_driven_by_the_catalog_and_combine_with_files():
    rules = [{"name": "maintenance", "title_types": [], "title_prefixes": ["chore(build)"],
              "files": []}, *RULES]
    assert labels.suggested_labels("chore(build)!: refresh packaging", [], rules) == {"maintenance"}
    assert labels.suggested_labels("chore(release): prepare version", [{"filename": "README.md"}], rules) == {
        "skip-changelog", "documentation",
    }


def test_manual_remove_and_readd_during_delete_restores_the_selected_label():
    api = GitHub()
    raced = False

    def concurrent(endpoint, *, method="GET", payload=None):
        nonlocal raced
        if endpoint == f"{ROOT}/issues/12/labels/bug" and method == "DELETE" and not raced:
            api.events.extend([event("bug", "unlabeled", "maintainer"),
                               event("bug", "labeled", "maintainer")])
            raced = True
        return api(endpoint, method=method, payload=payload)

    update = labels.apply_labels("Solin-Software/Solin", 12, RULES, concurrent)
    assert "bug" in {label["name"] for label in api.pull["labels"]}
    assert update.remove == ()
    assert api.writes[-1] == (f"{ROOT}/issues/12/labels", "POST", {"labels": ["bug"]})


def test_rerun_recovers_a_manual_choice_after_an_interrupted_reconciliation():
    api = GitHub()
    api.pull.update(title="Free title", labels=[])
    api.events.extend([event("bug", "unlabeled", "maintainer"),
                       event("bug", "labeled", "maintainer"), event("bug", "unlabeled")])
    api.files = [{"filename": "src/main.py"}]
    assert labels.apply_labels("Solin-Software/Solin", 12, RULES, api) == labels.LabelUpdate((), ())
    assert api.pull["labels"] == [{"name": "bug"}]


def test_manual_removal_during_automatic_add_is_restored():
    api = GitHub()
    raced = False

    def concurrent(endpoint, *, method="GET", payload=None):
        nonlocal raced
        if endpoint == f"{ROOT}/issues/12/labels" and method == "POST" and not raced:
            api.events.extend([event("enhancement", "labeled", "maintainer"),
                               event("enhancement", "unlabeled", "maintainer")])
            raced = True
        return api(endpoint, method=method, payload=payload)

    update = labels.apply_labels("Solin-Software/Solin", 12, RULES, concurrent)
    assert "enhancement" not in {label["name"] for label in api.pull["labels"]}
    assert update.add == ("documentation",)
    assert api.writes[-1] == (f"{ROOT}/issues/12/labels/enhancement", "DELETE", None)


def test_manual_removal_while_missing_labels_are_created_is_rechecked_before_writing():
    api = GitHub()
    api.available = []

    def concurrent(endpoint, *, method="GET", payload=None):
        if endpoint == f"{ROOT}/labels" and method == "POST" and payload["name"] == "enhancement":
            api.events.append(event("enhancement", "unlabeled", "maintainer"))
        return api(endpoint, method=method, payload=payload)

    update = labels.apply_labels("Solin-Software/Solin", 12, RULES, concurrent)
    assert update.add == ("documentation",)
    assert "enhancement" not in {label["name"] for label in api.pull["labels"]}


def test_manual_decision_changing_again_during_repair_uses_the_latest_choice():
    api = GitHub()
    api.events.append(event("bug", "unlabeled", "maintainer"))
    repaired = False

    def concurrent(endpoint, *, method="GET", payload=None):
        nonlocal repaired
        if endpoint == f"{ROOT}/issues/12/labels/bug" and method == "DELETE" and not repaired:
            api.events.append(event("bug", "labeled", "maintainer"))
            repaired = True
        return api(endpoint, method=method, payload=payload)

    assert labels.restore_concurrent_decisions(concurrent, ROOT, 12, {"bug"}) == {"bug"}
    assert "bug" in {label["name"] for label in api.pull["labels"]}


def test_repeated_concurrent_edits_fail_instead_of_looping_forever():
    api = GitHub()
    api.events.append(event("bug", "unlabeled", "maintainer"))

    def concurrent(endpoint, *, method="GET", payload=None):
        if endpoint == f"{ROOT}/issues/12/labels/bug" and method == "DELETE":
            api.events.append(event("bug", "labeled", "maintainer"))
        elif endpoint == f"{ROOT}/issues/12/labels" and method == "POST":
            api.events.append(event("bug", "unlabeled", "maintainer"))
        return api(endpoint, method=method, payload=payload)

    with pytest.raises(RuntimeError, match="kept changing"):
        labels.restore_concurrent_decisions(concurrent, ROOT, 12, {"bug"})
    assert len(api.writes) == 3


def test_missing_labels_are_created_without_overwriting_existing_label_metadata():
    api = GitHub()
    api.available = [entry for entry in api.available if entry["name"] != "enhancement"]
    labels.apply_labels("Solin-Software/Solin", 12, RULES, api)
    rule = next(rule for rule in RULES if rule["name"] == "enhancement")
    assert api.writes[0] == (f"{ROOT}/labels", "POST", {
        key: rule[key] for key in ("name", "color", "description")
    })
    assert all(method != "PATCH" for _, method, _ in api.writes)


@pytest.mark.parametrize("status", [422, 403])
def test_concurrent_label_creation_recovers_only_a_confirmed_validation_conflict(status):
    api = GitHub()
    api.available = []
    error = subprocess.CalledProcessError(1, "gh", stderr=f"HTTP {status}")
    lookups = []

    def concurrent(endpoint, *, method="GET", payload=None):
        if endpoint == f"{ROOT}/labels" and method == "POST":
            raise error
        if endpoint.startswith(f"{ROOT}/labels/"):
            lookups.append(endpoint)
            return {"name": endpoint.rsplit("/", 1)[1]}
        return api(endpoint, method=method, payload=payload)

    if status == 422:
        update = labels.apply_labels("Solin-Software/Solin", 12, RULES, concurrent)
        assert len(lookups) == 2
        assert update.add == ("documentation", "enhancement")
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            labels.apply_labels("Solin-Software/Solin", 12, RULES, concurrent)
        assert caught.value is error
        assert lookups == []
        assert api.writes == []


def test_label_creation_validation_failure_is_not_suppressed_without_confirmation():
    api = GitHub()
    api.available = []
    error = subprocess.CalledProcessError(1, "gh", stderr="HTTP 422")

    def invalid(endpoint, *, method="GET", payload=None):
        if endpoint == f"{ROOT}/labels" and method == "POST":
            raise error
        if endpoint.startswith(f"{ROOT}/labels/"):
            return {}
        return api(endpoint, method=method, payload=payload)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        labels.apply_labels("Solin-Software/Solin", 12, RULES, invalid)
    assert caught.value is error
    assert api.writes == []


def test_events_are_paginated_before_deciding_whether_a_label_is_manually_owned():
    api = GitHub()
    api.events = [{"event": "commented"}] * 100

    def paginated(endpoint, **kwargs):
        if endpoint == f"{ROOT}/issues/12/events?per_page=100&page=2":
            return [event("bug", actor="maintainer")]
        return api(endpoint, **kwargs)

    update = labels.apply_labels("Solin-Software/Solin", 12, RULES, paginated)
    assert update.remove == ()


def test_pagination_retains_all_changed_files():
    batches = {1: [{"filename": f"src/{n}.py"} for n in range(100)],
               2: [{"filename": "README.md"}]}
    seen = []

    def api(endpoint):
        seen.append(endpoint)
        return batches[int(endpoint.rsplit("=", 1)[1])]

    assert len(list(labels.pages(api, "files"))) == 101
    assert seen == ["files?per_page=100&page=1", "files?per_page=100&page=2"]


@pytest.mark.parametrize("endpoint", [
    f"{ROOT}/pulls/12/files?per_page=100&page=1",
    f"{ROOT}/issues/12/events?per_page=100&page=1",
    f"{ROOT}/labels?per_page=100&page=1",
])
def test_api_failure_never_produces_a_partial_classification(endpoint):
    api = GitHub()
    api.failure = endpoint
    with pytest.raises(RuntimeError, match="API unavailable"):
        labels.apply_labels("Solin-Software/Solin", 12, RULES, api)
    assert api.writes == []


def test_file_limit_or_concurrent_file_change_fails_without_mutations():
    api = GitHub()
    api.pull["changed_files"] = 3001
    with pytest.raises(ValueError, match="Incomplete"):
        labels.apply_labels("Solin-Software/Solin", 12, RULES, api)
    assert api.writes == []


def test_a_queued_run_for_a_closed_pull_request_does_nothing():
    api = GitHub()
    api.pull["state"] = "closed"
    assert labels.apply_labels("Solin-Software/Solin", 12, RULES, api) == labels.LabelUpdate((), ())
    assert len(api.calls) == 1


@pytest.mark.parametrize("batch", [{}, [None]])
def test_malformed_api_pages_fail(batch):
    with pytest.raises(ValueError, match="Invalid GitHub listing"):
        list(labels.pages(lambda endpoint: batch, "files"))


@pytest.mark.parametrize(("repository", "number"), [("../bad", 12), ("owner/repo", 0)])
def test_invalid_identity_is_rejected_before_api_access(repository, number):
    api = GitHub()
    with pytest.raises(ValueError, match="identity"):
        labels.apply_labels(repository, number, RULES, api)
    assert api.calls == []


def test_transport_passes_json_through_stdin_without_a_shell(monkeypatch):
    captured = {}
    payload = {"labels": ["$(unsafe)`text"]}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(labels.subprocess, "run", run)
    assert labels.gh_api("endpoint", method="POST", payload=payload) == {}
    assert json.loads(captured["input"]) == payload
    assert captured["command"][-2:] == ["--input", "-"]
    assert "shell" not in captured
    assert captured["check"] is True

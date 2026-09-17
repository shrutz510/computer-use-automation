"""Trace -> artifact: parameterization, canonicalized checkpoints, risk, versioning."""

import pytest

from cua.agent.trace import FrameState, Trace, TraceStep
from cua.artifact import store
from cua.recorder import derive_name, record
from cua.surface import CellStrategy, LabelStrategy, RoleStrategy, Target

SEARCH = [FrameState(name="content", url="/members/search", title="Member Search")]
DETAIL = [FrameState(name="content", url="/members/10023", title="Member Detail")]


def trace(status: str = "success") -> Trace:
    steps = [
        TraceStep(index=1, tool="type_text", value="10023", param_name="member_id",
                  target=Target(frame="content", strategies=[LabelStrategy(text="Member ID")]),
                  element_role="textbox", frames_before=SEARCH, frames_after=SEARCH),
        TraceStep(index=2, tool="click", element_name="Search", element_form_method="post",
                  target=Target(frame="content", strategies=[RoleStrategy(role="button", name="Search")]),
                  element_role="button", frames_before=SEARCH, frames_after=DETAIL),
        TraceStep(index=3, tool="click", element_name="Confirm", element_form_method="post",
                  target=Target(frame="content", strategies=[RoleStrategy(role="button", name="Confirm")]),
                  element_role="button", frames_before=DETAIL, frames_after=DETAIL),
        TraceStep(index=4, tool="extract", value="$10,527.64", output_name="savings_balance", output_type="decimal",
                  target=Target(frame="content", strategies=[CellStrategy(row_header="Share Savings", column_header="Balance")]),
                  element_role="cell", frames_before=DETAIL, frames_after=DETAIL),
        TraceStep(index=5, tool="click", element_name="New Search", ok=False, error="click failed",
                  target=Target(frame="content", strategies=[RoleStrategy(role="link", name="New Search")]),
                  frames_before=DETAIL, frames_after=DETAIL),
    ]
    return Trace(run_id="discovery-test", goal="look up member 10023 and read their savings balance",
                 app="legacycore", base_url="http://localhost:5001", entry_route="/", model="gemini-3.6-flash",
                 started_at="2026-09-15T00:00:00Z", status=status, steps=steps)


@pytest.fixture(scope="module")
def pack():
    return store.load_pack("legacycore")


@pytest.fixture
def artifact(pack):
    return record(trace(), pack)


def test_derive_name_drops_filler_and_ids():
    assert derive_name("look up member 10023 and read their savings balance") == "member_savings_balance"


def test_typed_value_becomes_an_input_reference(artifact):
    assert artifact.steps[0].value == "{{inputs.member_id}}"
    assert artifact.inputs["member_id"].pattern == r"^\d{5}$"
    assert artifact.inputs["member_id"].example == "10023"


def test_outputs_are_typed_and_linked_to_their_step(artifact):
    out = artifact.outputs["savings_balance"]
    assert out.type == "decimal"
    assert out.source_step == artifact.steps[3].id
    assert artifact.success.outputs_present == ["savings_balance"]


def test_checkpoint_is_canonicalized(artifact):
    checkpoint = artifact.steps[1].checkpoint
    assert checkpoint.title == "Member Detail"
    assert checkpoint.url_matches == r"^/members/[^/]+$"  # not the recorded member id
    assert artifact.steps[0].checkpoint is None  # typing changed nothing to verify


def test_risk_is_conservative(artifact):
    assert [s.risk for s in artifact.steps] == ["safe", "reversible", "irreversible", "safe"]
    assert artifact.capability.risk_level == "irreversible"
    assert artifact.capability.status == "draft"


def test_failed_actions_are_not_recorded(artifact):
    assert len(artifact.steps) == 4
    assert all("new_search" not in s.id for s in artifact.steps)


def test_detectors_ship_with_the_artifact(artifact):
    assert artifact.detectors_pack == "legacycore@1"
    assert "MEMBER_NOT_FOUND" in [o.code for o in artifact.detectors.business_outcomes]
    assert "system_notice" in [h.id for h in artifact.detectors.recoverable]


def test_only_successful_runs_can_be_recorded(pack):
    with pytest.raises(ValueError):
        record(trace(status="escalated"), pack)


def test_save_load_round_trip_and_version_bump(artifact, tmp_path):
    first = store.save(artifact, root=tmp_path)
    assert first.name == "v1.yaml"
    assert store.load(first) == artifact
    # The recorded value survives only as the input's example and in provenance (the raw goal),
    # never in a step, a checkpoint or the description.
    body = [l for l in first.read_text().splitlines() if "example:" not in l and "goal:" not in l]
    assert "10023" not in "\n".join(body)
    assert artifact.capability.description == "look up member {{inputs.member_id}} and read their savings balance"

    artifact.capability.version = store.next_version("legacycore", artifact.capability.name, root=tmp_path)  # 2
    second = store.save(artifact, root=tmp_path)
    assert second.name == "v2.yaml"
    assert store.load_latest("legacycore", artifact.capability.name, root=tmp_path)[0] == second

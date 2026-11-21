from copy import deepcopy

from audit.provenance import build_base, build_workflow, with_prompts


def test_fingerprint_changes_with_prompts_code_and_model(tmp_path):
    source = tmp_path / "backend" / "audit"
    source.mkdir(parents=True)
    prompt = source / "prompts.py"
    prompt.write_text('PROMPT = "initial"')
    one = build_workflow({"model_name": "model-a"}, source_root=source)
    assert one["id"] == build_workflow({"model_name": "model-a"}, source_root=source)["id"]
    prompt.write_text('PROMPT = "revised"')
    two = build_workflow({"model_name": "model-a"}, source_root=source)
    assert one["id"] != two["id"]
    assert one["definition"]["sources"]["prompts.py"] == 'PROMPT = "initial"'
    assert two["id"] != build_workflow({"model_name": "model-b"}, source_root=source)["id"]
    (tmp_path / "uv.lock").write_text("version = 1")
    assert two["id"] != build_workflow({"model_name": "model-a"}, source_root=source)["id"]


def test_fingerprint_never_includes_api_secrets_or_data_paths(tmp_path):
    assert build_workflow({"model_api_key": "private", "data_dir": "/private"}, source_root=tmp_path)["id"] == build_workflow({}, source_root=tmp_path)["id"]


def test_search_compose_is_a_behavior_dependency(tmp_path):
    source = tmp_path / "backend" / "audit"
    source.mkdir(parents=True)
    search = tmp_path / "compose.search.yaml"
    search.write_text("services: {}")
    one = build_workflow({}, source_root=source)
    assert one["definition"]["dependencies"][search.name] == "services: {}"
    search.write_text("services: {fabricated: {}}")
    assert build_workflow({}, source_root=source)["id"] != one["id"]


def test_split_provenance_preserves_existing_fingerprint_and_copies_base(tmp_path):
    from audit.workflow.prompt_registry import DEFAULTS
    source = tmp_path / "backend" / "audit"
    source.mkdir(parents=True)
    (source / "engine.py").write_text('RULE = "fabricated"\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    config = {"model_name": "fabricated", "model_api_key": "excluded"}
    prompts = {**DEFAULTS, "relevance": DEFAULTS["relevance"] + "\nFabricated revision"}
    base = build_base(config, source_root=source)
    original = deepcopy(base)
    split = with_prompts(base, prompts)
    combined = build_workflow(config, prompts=prompts, source_root=source)
    assert split["definition"] == combined["definition"]
    assert split["id"] == combined["id"]
    assert "created_at" in split
    split["definition"]["steps"][0]["name"] = "changed"
    split["definition"]["sources"]["engine.py"] = "changed"
    split["definition"]["dependencies"]["uv.lock"] = "changed"
    split["definition"]["settings"]["model_name"] = "changed"
    split["definition"]["prompts"]["relevance"] = "changed"
    assert base == original
    assert prompts["relevance"] != "changed"


def test_active_prompt_texts_are_content_addressed_and_defaults_exact(tmp_path):
    from audit.workflow.prompt_registry import DEFAULTS
    one = build_workflow({}, source_root=tmp_path)
    assert one["definition"]["format_version"] == 3
    assert one["definition"]["prompts"] == DEFAULTS
    assert one["id"] == build_workflow({}, prompts=dict(DEFAULTS), source_root=tmp_path)["id"]
    texts = {**DEFAULTS, "relevance": DEFAULTS["relevance"] + "\nFabricated revision"}
    assert one["id"] != build_workflow({}, prompts=texts, source_root=tmp_path)["id"]
    assert one["id"] == build_workflow({}, prompts=DEFAULTS, source_root=tmp_path)["id"]


def test_historic_workflow_definition_accepts_v1_without_prompts():
    from audit.schemas import WorkflowDefinition
    definition = build_workflow({})["definition"]
    definition.pop("prompts")
    definition["format_version"] = 1
    assert WorkflowDefinition.model_validate(definition).prompts == {}


def test_model_mapping_is_content_addressed_without_assignment_metadata(tmp_path):
    from audit.workflow.prompt_registry import DEFAULTS
    base = build_base({"model_name": "fabricated-default", "model_api_key": "private-value"}, source_root=tmp_path)
    models = deepcopy(base["models"])
    one = with_prompts(base, DEFAULTS, models)
    models["steps"]["relevance"] = "fabricated-screen"
    assert with_prompts(base, DEFAULTS, models)["id"] != one["id"]
    models["steps"]["relevance"] = models["default"]
    assert with_prompts(base, DEFAULTS, models)["id"] == one["id"]
    assert "private-value" not in str(one["definition"])
    assert "model_api_key" not in str(one["definition"])
    assert "similarity" not in models["steps"]

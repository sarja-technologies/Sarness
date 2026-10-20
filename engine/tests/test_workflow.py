import threading

import pytest

from sarness_engine.workflow import graph as wf
from sarness_engine.workflow import templates
from sarness_engine.workflow.executor import NodeCache, execute
from sarness_engine.workflow.registry import node
from sarness_engine.workflow.types import INT, Cancelled, Param, Socket, WorkflowError, compatible

MODELS = {
    "sdxl-turbo": {"id": "sdxl-turbo", "name": "SDXL Turbo", "category": "image", "family": "sdxl"},
    "wan": {"id": "wan", "name": "Wan", "category": "video", "family": "wan"},
    "lora1": {"id": "lora1", "name": "LoRA", "category": "lora", "family": "lora"},
}
lookup = MODELS.get

# ---- test-only nodes ---------------------------------------------------
CALLS = {"count": 0}


@node("test.const", title="Const", category="Test", outputs=[Socket("v", INT)], params=[Param("v", "int", 1)])
def _const(ctx, inputs, params):
    CALLS["count"] += 1
    ctx.annotate(const=params["v"])
    return {"v": params["v"]}


@node("test.add", title="Add", category="Test", inputs=[Socket("a", INT), Socket("b", INT)],
      outputs=[Socket("v", INT)])
def _add(ctx, inputs, params):
    return {"v": inputs["a"] + inputs["b"]}


@node("test.boom", title="Boom", category="Test", inputs=[Socket("a", INT)], outputs=[Socket("v", INT)])
def _boom(ctx, inputs, params):
    raise ValueError("kaboom")


@node("test.slow", title="Slow", category="Test", inputs=[Socket("a", INT)], outputs=[Socket("v", INT)])
def _slow(ctx, inputs, params):
    for i in range(1000):
        ctx.progress(i, 1000)
    return {"v": 0}


@node("test.sink", title="Sink", category="Test", inputs=[Socket("a", INT)], output_node=True, cacheable=False)
def _sink(ctx, inputs, params):
    return {"_outputs": [{"kind": "value", "value": inputs["a"]}]}


def G(nodes, edges):
    return {"format": "sarness.workflow", "version": 1, "nodes": nodes, "edges": edges}


def N(id, type, **params):
    return {"id": id, "type": type, "params": params}


def E(s, so, t, ti):
    return {"id": f"{s}-{t}-{ti}", "source": s, "source_output": so, "target": t, "target_input": ti}


class Svc:
    pass


def run(g, cache=None, cancel=None):
    events = []
    out = execute(g, job_id="j", services=Svc(), emit=events.append, cancel_event=cancel or threading.Event(),
                  cache=cache)
    return out, events


# ---- validation ----------------------------------------------------------
def test_socket_compat():
    assert compatible("INT", "FLOAT")
    assert not compatible("FLOAT", "INT")
    assert compatible("IMAGE", "ANY")
    assert not compatible("IMAGE", "LATENT")


def test_simple_image_workflow_is_valid():
    g = templates.image_workflow({"model": "sdxl-turbo", "prompt": "a cat"})
    issues = wf.validate(g, lookup)
    assert [i.message for i in issues] == []
    order = wf.order(g)
    assert order.index("model") < order.index("prompt") < order.index("sampler") < order.index("decode") < order.index("save")


def test_validation_catches_type_mismatch_and_missing_inputs():
    g = templates.image_workflow({"model": "sdxl-turbo", "prompt": "x"})
    # Wire the prompt conditioning into the decoder's latent input: CONDITIONING -> LATENT.
    g["edges"] = [e for e in g["edges"] if not (e["target"] == "decode" and e["target_input"] == "latent")]
    g["edges"].append(E("prompt", "conditioning", "decode", "latent"))
    msgs = [i.message for i in wf.validate(g, lookup)]
    assert any("cannot connect CONDITIONING to LATENT" in m for m in msgs)


def test_validation_unknown_model_and_category():
    g = templates.image_workflow({"model": "wan", "prompt": "x"})
    msgs = [i.message for i in wf.validate(g, lookup)]
    assert any("video model, expected image" in m for m in msgs)
    g = templates.image_workflow({"model": "nope", "prompt": "x"})
    assert any("not installed" in i.message for i in wf.validate(g, lookup))


def test_validation_checks_frames_against_the_video_model():
    models = {**MODELS,
              "i2v": {"id": "i2v", "name": "Wan I2V", "category": "video", "family": "wan",
                      "capabilities": {"modes": ["i2v"]}},
              "both": {"id": "both", "name": "H3", "category": "video", "family": "wan",
                       "capabilities": {"modes": ["t2v", "i2v"], "end_frame": True}}}
    msgs = lambda simple: [i.message for i in wf.validate(templates.video_workflow(simple), models.get)]
    assert msgs({"model": "i2v", "prompt": "x"}) == ["Wan I2V needs a start frame (image-to-video only)"]
    assert msgs({"model": "i2v", "prompt": "x", "start_image": "f_1"}) == []
    assert msgs({"model": "wan", "prompt": "x", "start_image": "f_1"}) == ["Wan does not support image-to-video"]
    assert msgs({"model": "i2v", "prompt": "x", "start_image": "f_1", "end_image": "f_2"}) == \
        ["Wan I2V does not support end frames"]
    assert msgs({"model": "both", "prompt": "x"}) == msgs({"model": "both", "prompt": "x", "start_image": "f_1",
                                                           "end_image": "f_2"}) == []


def test_cycle_detected():
    g = G([N("a", "test.add"), N("b", "test.add"), N("s", "test.sink")],
          [E("a", "v", "b", "a"), E("b", "v", "a", "a"), E("b", "v", "s", "a")])
    msgs = [i.message for i in wf.validate(g)]
    assert any("cycle" in m for m in msgs)


def test_param_range_validation():
    g = templates.image_workflow({"model": "sdxl-turbo", "prompt": "x", "steps": 1000})
    assert any("steps" in i.message and "≤" in i.message for i in wf.validate(g, lookup))


def test_seed_resolution():
    g = templates.image_workflow({"model": "sdxl-turbo", "prompt": "x", "seed": -1})
    r = wf.resolve_seeds(g)
    seed = next(n for n in r["nodes"] if n["id"] == "sampler")["params"]["seed"]
    assert seed >= 0
    assert next(n for n in g["nodes"] if n["id"] == "sampler")["params"]["seed"] == -1


def test_resolved_seeds_fit_signed_int32_for_image_and_video():
    for build, simple in ((templates.image_workflow, {"model": "sdxl-turbo", "prompt": "x", "seed": -1}),
                          (templates.video_workflow, {"model": "wan", "prompt": "x", "seed": -1})):
        g = build(simple)
        for _ in range(500):
            r = wf.resolve_seeds(g)
            seeds = [n["params"]["seed"] for n in r["nodes"] if "seed" in (n.get("params") or {})]
            assert seeds and all(0 <= s <= wf.SEED_MAX for s in seeds)


def test_clamp_seed_keeps_in_range_values():
    assert wf.clamp_seed(5) == 5 and wf.clamp_seed(wf.SEED_MAX) == wf.SEED_MAX
    assert 0 <= wf.clamp_seed(2**32 - 1) <= wf.SEED_MAX and 0 <= wf.clamp_seed(wf.SEED_MAX + 1) <= wf.SEED_MAX


# ---- simple <-> workflow ------------------------------------------------------
@pytest.mark.parametrize("simple", [
    {"model": "sdxl-turbo", "prompt": "a cat", "steps": 4, "guidance": 0.0, "width": 512, "height": 512},
    {"model": "sdxl-turbo", "prompt": "a cat", "reference_image": "f_1", "denoise": 0.5},
    {"model": "sdxl-turbo", "prompt": "a cat", "reference_image": "f_1", "mask_image": "f_2"},
    {"model": "sdxl-turbo", "prompt": "a", "loras": [{"id": "lora1", "strength": 0.8}], "scheduler": "lcm"},
    {"model": "sdxl-turbo", "prompt": "a", "upscale": {"model": "up", "factor": 2}},
])
def test_image_roundtrip(simple):
    g = templates.image_workflow(simple)
    rep = templates.to_simple(g)
    assert rep["representable"], rep["reasons"]
    for k, v in simple.items():
        assert rep["simple"][k] == v, k


def test_video_roundtrip():
    simple = {"model": "wan", "prompt": "waves", "frames": 33, "fps": 16, "start_image": "f_9"}
    rep = templates.to_simple(templates.video_workflow(simple))
    assert rep["representable"], rep["reasons"]
    assert rep["simple"]["start_image"] == "f_9" and rep["simple"]["frames"] == 33


def test_extra_node_not_representable():
    g = templates.image_workflow({"model": "sdxl-turbo", "prompt": "a"})
    g["nodes"].append(N("blur", "image.blur", radius=2))
    g["edges"] = [e for e in g["edges"] if e["target"] != "save"]
    g["edges"] += [E("decode", "image", "blur", "image"), E("blur", "image", "save", "image")]
    rep = templates.to_simple(g)
    assert not rep["representable"]
    assert any("image.blur" in r for r in rep["reasons"])


def test_rewired_graph_not_representable():
    g = templates.image_workflow({"model": "sdxl-turbo", "prompt": "a"})
    g["nodes"].append(N("latent2", "latent.empty", width=64, height=64, batch_size=1))
    rep = templates.to_simple(g)
    assert not rep["representable"]


# ---- executor ---------------------------------------------------------------
def test_execute_order_outputs_and_lineage():
    g = G([N("a", "test.const", v=2), N("b", "test.const", v=3), N("sum", "test.add"), N("out", "test.sink")],
          [E("a", "v", "sum", "a"), E("b", "v", "sum", "b"), E("sum", "v", "out", "a")])
    out, events = run(g)
    assert out["outputs"][0]["value"] == 5
    assert out["outputs"][0]["lineage"]["const"] in (2, 3)
    assert all(s["status"] == "done" for s in out["node_states"].values())
    assert any(e["type"] == "node" and e["status"] == "running" for e in events)


def test_cache_reuses_unchanged_nodes():
    cache = NodeCache()
    g = G([N("a", "test.const", v=2), N("b", "test.const", v=3), N("sum", "test.add"), N("out", "test.sink")],
          [E("a", "v", "sum", "a"), E("b", "v", "sum", "b"), E("sum", "v", "out", "a")])
    CALLS["count"] = 0
    run(g, cache)
    assert CALLS["count"] == 2
    g["nodes"][1]["params"]["v"] = 10
    out, _ = run(g, cache)
    assert CALLS["count"] == 3  # only 'b' re-ran
    assert out["node_states"]["a"]["status"] == "cached"
    assert out["outputs"][0]["value"] == 12


def test_error_is_attributed_and_downstream_skipped():
    g = G([N("a", "test.const"), N("x", "test.boom"), N("out", "test.sink")],
          [E("a", "v", "x", "a"), E("x", "v", "out", "a")])
    events = []
    with pytest.raises(WorkflowError) as ei:
        execute(g, job_id="j", services=Svc(), emit=events.append, cancel_event=threading.Event())
    assert ei.value.node_id == "x"
    assert "kaboom" in str(ei.value)
    statuses = {e["node_id"]: e["status"] for e in events if e["type"] == "node"}
    assert statuses["x"] == "error" and statuses["out"] == "skipped"


def test_cancellation_inside_node():
    g = G([N("a", "test.const"), N("s", "test.slow"), N("out", "test.sink")],
          [E("a", "v", "s", "a"), E("s", "v", "out", "a")])
    ev = threading.Event()
    calls = []

    def emit(e):
        calls.append(e)
        if e["type"] == "progress":  # events are throttled; cancel on the first
            ev.set()

    with pytest.raises(Cancelled):
        execute(g, job_id="j", services=Svc(), emit=emit, cancel_event=ev)


def test_unused_branches_are_pruned():
    g = G([N("a", "test.const"), N("dangling", "test.boom"), N("out", "test.sink")],
          [E("a", "v", "out", "a"), E("a", "v", "dangling", "a")])
    out, _ = run(g)  # the failing node is not an ancestor of an output, so it never runs
    assert out["outputs"][0]["value"] == 1

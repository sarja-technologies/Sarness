"""A GPU-free experiment handler for exercising the experiment worker process in tests."""

import os
import time


class Boom(Exception):
    pass


class Cancelled(Exception):
    pass


class Handler:
    def __init__(self, options, emit):
        self.options = options
        self.released = []

    def run(self, op, p, cancelled, progress):
        if op == "double":
            return {"y": p["x"] * self.options["scale"], "pid": os.getpid()}
        if op == "slow":
            for i in range(p["steps"]):
                if cancelled():
                    raise Cancelled("stopped")
                progress(i + 1, p["steps"])
                time.sleep(0.02)
            return {"done": True}
        if op == "fail":
            raise Boom("it broke")
        if op == "crash":
            os._exit(3)
        raise ValueError(op)

    def release(self, level):
        self.released.append(level)

    def status(self):
        return {"released": list(self.released)}


def errors():
    return {Cancelled: "cancelled", Boom: "boom"}


class FakePaintbrush:
    """Stands in for the paintbrush GPU handler: 'renders' the interface colours of the label map."""

    def __init__(self, options, emit):
        self.calls = 0

    def run(self, op, p, cancelled, progress):
        import io

        from PIL import Image

        from sarness_engine.experiments.paintbrush import masks as MK

        if op == "warmup":
            return {"status": self.status()}
        labels = p["labels"]
        h, w = labels.shape
        img = Image.fromarray(MK.to_color(labels))
        if op == "refine":
            for i in range(10):
                if cancelled():
                    raise Cancelled("stopped")
                progress(i + 1, 10)
                time.sleep(0.05)
            w, h = int(p["width"]), int(p["height"])
            img = img.resize((w, h), Image.NEAREST)
        self.calls += 1
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return {"image": buf.getvalue(), "format": "png", "width": w, "height": h,
                "timings": {"load_ms": 0.0, "live_ms": 1.0}, "peak_vram_mb": 0}

    def release(self, level):
        pass

    def status(self):
        return {"device": "cpu", "calls": self.calls}

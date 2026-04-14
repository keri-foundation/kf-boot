from __future__ import annotations
import falcon
from keri.core.counting import Counter

class OnboardingEnd:
    def __init__(self, ctx):
        self.ctx = ctx
        self.parser = ctx.habery.psr      # Parser wired to Kevery
        self.exchanger = ctx.exchanger    # BootExchanger (exc in Kevery)

    def on_post(self, req, rep):
        # 1. Read full body in a validator-safe way
        length = req.content_length or 0
        raw = req.bounded_stream.read(length)

        if not raw:
            raise falcon.HTTPBadRequest(
                title="Missing CESR message",
                description="Expected CESR-over-HTTP payload",
            )

        # 2. Optional: quick CESR sanity check for non-JSON
        if not raw.startswith(b"{"):
            try:
                Counter(qb64b=raw[:4], strip=False)
            except Exception:
                raise falcon.HTTPBadRequest(
                    title="Invalid CESR message",
                    description="Malformed CESR counter",
                )

        ims = bytearray(raw)
        # 3. Let the parser + Kevery do all routing (KEL/TEL/QRY/RPY/EXN)
        try:
            # Kevery is already wired with exc=self.exchanger and kramer
            self.parser.parse(ims=ims, exc=self.exchanger)
        except Exception as exc:
            raise falcon.HTTPBadRequest(
                title="Invalid CESR message",
                description=str(exc),
            )

        # 5. If BootExchanger produced a reply, return it
        for cue in list(self.exchanger.cues):
            if cue.get("kin") == "reply":
                rep.data = cue["msg"]
                rep.content_type = "application/cesr"
                rep.status = falcon.HTTP_200
                self.exchanger.cues.clear()
                return

        # 6. No reply (e.g., inception) → just 200 OK
        rep.status = falcon.HTTP_200
"""
laya_worker.py -- the private local Laya process for the experimental
fixed-30 selector. Run it with the SEPARATE Laya virtualenv's python; the
normal harness runtime never imports torch or laya.

    <laya-venv>/python laya_worker.py <checkpoint_dir>

It loads the pinned checkpoint ONCE (laya_adapter.LayaScorer.load: exact
package version, pinned commit, weight SHA-256 verified, local directory,
CPU), blocks all network access at the socket layer, then serves JSON lines
over its own stdin/stdout PIPES -- no port, no HTTP, no public endpoint. It
exits when stdin closes, so it can never outlive the controller.

Protocol (one JSON object per line):
  worker -> controller, once after load:
      {"event": "ready", "package_version": ..., "checkpoint": ...,
       "revision": ..., "question": ..., "settings": ...}
  controller -> worker:  {"id": N, "cards": [{"row_id": ..., <7 numeric fields>}, ...]}
  worker -> controller:  {"id": N, "records": [{"row_id", "review_probability",
                                                 "answer_confidence"}, ...]}
                      or {"id": N, "error": "<message>"}
Cards are rebuilt through build_card, so a card carrying any non-numeric or
unknown field is rejected here too. Output is two numbers per card -- no text.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from typing import IO, Any

from laya_selector import CardScorer, ScorerMetadata, build_card


def _deny_network() -> None:
    """Scoring must be local: make any socket connect / DNS lookup fail."""
    def deny(*_a: Any, **_k: Any):
        raise RuntimeError("network access is disabled in the Laya worker")
    socket.socket.connect = deny          # type: ignore[method-assign]
    socket.getaddrinfo = deny             # type: ignore[assignment]


def serve(scorer: CardScorer, stdin: IO[str], stdout: IO[str]) -> None:
    """Handshake, then answer requests until stdin closes. Pure protocol
    loop over injected streams so it can be tested with a fake scorer."""
    meta: ScorerMetadata = scorer.metadata
    stdout.write(json.dumps({
        "event": "ready", "package_version": meta.package_version,
        "checkpoint": meta.checkpoint, "revision": meta.revision,
        "question": meta.question, "settings": meta.settings,
    }) + "\n")
    stdout.flush()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            cards = [
                build_card(c["row_id"], {k: v for k, v in c.items() if k != "row_id"})
                for c in req["cards"]
            ]
            records = scorer.score(cards)
            reply = {"id": req_id, "records": [
                {"row_id": r.row_id, "review_probability": r.review_probability,
                 "answer_confidence": r.answer_confidence} for r in records]}
        except Exception as exc:  # report, never die silently: the controller fails closed
            reply = {"id": req_id, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: laya_worker.py <checkpoint_dir>", file=sys.stderr)
        return 2
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    # Protocol owns the real stdout; anything a library prints goes to stderr.
    proto_out, sys.stdout = sys.stdout, sys.stderr
    from laya_adapter import LayaScorer

    scorer = LayaScorer.load(argv[1])
    _deny_network()
    serve(scorer, sys.stdin, proto_out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

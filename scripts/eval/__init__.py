"""LAYER 3 - agent evaluation for the SAP chat.

Measures what Layers 1 and 2 structurally cannot: whether Nova ROUTES to the
right tool and NARRATES the result without inventing anything.

The central design choice is that the SAP tools are stubbed with recorded
fixtures. This eval is not asking "is SAP up?" - Layer 2 answers that. Mixing
the two produces a score that moves for reasons you cannot attribute. Here,
every input to the model is fixed, so a score change means the model or the
prompt changed.

Everything else is the real production path: the real system prompt, the real
tool specs, the real Bedrock Converse tool-use loop, the real prompt builder.

Run:
    python -m scripts.eval                 # full run against Bedrock
    python -m scripts.eval --repeats 5     # tighter flakiness measurement
    python -m scripts.eval --dry-run       # harness self-check, no Bedrock calls
    python -m scripts.eval --baseline      # write data/eval_baseline.json
"""

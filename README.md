# X-to-LinkedIn Content Radar

A small, gated Python research workflow that uses the xAI SDK's X Search tool
to find developing public arguments on X and turn them into review-ready
LinkedIn concept briefs for a human owner. It never publishes or schedules.

What it demonstrates:

- **Tool-using LLM calls with hard gates.** Live runs need a prepared, digested
  request packet plus an explicit `--approval-id`; each packet is single-use.
- **Evidence over vibes.** A candidate qualifies only with three distinct,
  attributable source posts whose URL and excerpt match the tool output.
  Empty or weak results render as `insufficient_signal`; nothing is invented.
- **Cost and failure discipline.** One agentic turn, no retries, bounded
  output, a pricing snapshot and cost reservation in every receipt, and
  sanitized streaming progress logs that never record model reasoning.
- **Provider-free tests.** 39 unit tests cover schema validation, ranking,
  rendering, receipts, gating, and SDK request construction without network
  calls.

## Requirements

Python 3.10+ and `xai-sdk==1.20.0` (see `requirements.txt`).

## Run offline (no API key)

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python radar.py fixture --config fixture/config.json \
  --provider-response fixture/qualified-synthetic-response.json \
  --out test-output/fixture
```

Each run writes `edition.json`, `review.md`, and `review.html` to the output
directory. The fixtures are synthetic.

## Tests

```bash
RADAR_TEST_NATIVE_SDK=1 python -m unittest discover -s tests -v
```

## Live runs (paid, opt-in)

```bash
python radar.py prepare-live --config fixture/config.json --out runs/prepared-live
# review runs/prepared-live, then:
XAI_API_KEY=... python radar.py live --config fixture/config.json \
  --prepared-dir runs/prepared-live --out runs/<unique-run-id> \
  --allow-unknown-item-cost --approval-id '<human approval reference>'
```

`native-canary` and `native-stream-canary` make one tiny no-tool request to
check the SDK transport. Set `XAI_API_KEY` only in the process environment;
it is never written to receipts.

Edit `source_packet` in `fixture/config.json` to describe your own public
positions and claim limits.

## License

MIT

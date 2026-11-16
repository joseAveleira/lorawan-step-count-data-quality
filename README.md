# Data quality of step counts uploaded opportunistically over LoRaWAN

Code and data of a controlled experimental study on the quality of wearable
step-count data that reaches a clinical system when the device has only
intermittent LoRaWAN coverage. The study separates the quality of the data
from the quality of the radio link, and distinguishes an interval with zero
steps from an interval with no data.

The device extends the prototype described in:

> J. Aveleira-Mata, D. Narciandi-Rodríguez, S. Rubio-Martín, N. Prieto-Fernández,
> M. Bayón-Gutiérrez and I. García-Rodríguez, "Opportunistic LoRaWAN-based
> step-count monitoring for postoperative follow-up in low-connectivity rural
> settings," in *Proc. 39th IEEE Int. Symp. on Computer-Based Medical Systems
> (CBMS 2026)*, pp. 1394-1399. doi: [10.1109/CBMS69103.2026.00277](https://doi.org/10.1109/CBMS69103.2026.00277)

The manuscript that reports this study is under review. Its reference will be
added here when it is published.

## Contents

| Path | Contents |
|---|---|
| `software/firmware/hij_node/` | Node firmware (Heltec WiFi LoRa 32 V3, PlatformIO, RadioLib). Time-binned step counts, persistent buffer, four transmission strategies (S0 to S3), emulated connectivity scenarios and ground-truth log |
| `software/backend/` | Payload decoder, series reconstruction, acknowledgement logic, data-quality metrics and FHIR R4 mapping (`hij/`), with collectors for The Things Network and the gateway |
| `software/simulator/` | Discrete-event simulator with the same logic as the firmware, its validation against the hardware runs and the 30-day follow-up simulation |
| `software/fhir/` | Export of FHIR Bundles and validation with the official HL7 validator |
| `software/experiments/` | Experiment runner, campaign scheduler and the plan of each run |
| `software/analysis/` | Scripts that produce the tables and figures |
| `software/data/raw/` | Logs of the 29 hardware runs |
| `software/data/processed/` | Per-run bins and frames, metrics, simulation results and validation summaries |

Comments and log messages in the code are in Spanish.

## Data

Each folder in `software/data/raw/` is one hardware run, named
`<scenario>_<strategy>`:

- Scenarios: `C1` continuous coverage, `C2` daily visit, `C3-10` and `C3-30`
  burst losses (10 % and 30 %), `C4-24`, `C4-48` and `C4-72` outages of 24, 48
  and 72 h, `C5-reboot` and `C5-overflow` device faults, and `REAL-C4-24`, a
  24-h outage produced at the gateway instead of emulated in the node.
- Strategies: `S0` current state only, `S1` store-and-forward, `S2`
  store-and-forward with redundancy, `S3` store-and-forward with
  application-level acknowledgements. `S4-valid` is an additional check run
  that is not part of the comparison.

Runs use a 15x time compression: one bin lasts 60 s and represents 15 min.

| File | Contents |
|---|---|
| `plan.json` | Parameters and schedule of the run |
| `node.log` | Serial log of the node with host timestamps. `GT` lines are the ground truth (one per closed bin), `TX` lines are the frames (sent, suppressed or dropped) and `EV` lines are events |
| `ttn.jsonl` | Uplinks received by the network server (The Things Network) |
| `gateway.jsonl` | Packets of the node logged by the gateway (RSSI, SNR, frame counter) |
| `runner.log` | Log of the experiment runner |
| `DONE` | Marks a completed run |

Activity in the hardware runs is synthetic and deterministic. It is generated
in the firmware from a seed and can be regenerated with
`software/backend/hij/synthetic.py`. No data from patients or volunteers is
included.

The raw logs were edited before publication in two ways: packets from other
devices heard by the gateway were removed from `gateway.jsonl`, and local
paths and addresses were removed from `runner.log`. The metrics computed from
the published files are identical to those computed from the original files.

## Reproducing the results

Python 3.13 with the packages in `requirements.txt`.

```bash
pip install -r requirements.txt
```

Tests:

```bash
python -m pytest software/backend/tests software/simulator/test_simulator.py
```

Metrics of the hardware runs (`software/data/processed/metrics.csv` and the
per-run `_bins.csv` and `_frames.csv` files):

```bash
python software/backend/report.py --all
```

Validation of the simulator against the hardware runs:

```bash
python software/simulator/validate_lab.py
```

30-day follow-up simulation:

```bash
python software/simulator/sim30.py
```

Tables and figures (written to `HIJ_2026_extension/` next to `software/`):

```bash
python software/analysis/make_tables.py
```

```bash
python software/analysis/make_figures.py
```

The FHIR export (`software/fhir/export_validate.py`) needs Java and the
[HL7 FHIR validator](https://github.com/hapifhir/org.hl7.fhir.core/releases);
pass their locations with `--java` and `--validator`.

## Firmware

Copy `software/firmware/hij_node/include/secrets.example.h` to `secrets.h`,
fill in the OTAA credentials of the device and build with PlatformIO:

```bash
pio run -d software/firmware/hij_node -t upload
```

## License

Code is released under the MIT License (`LICENSE`). The data in
`software/data/` are released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

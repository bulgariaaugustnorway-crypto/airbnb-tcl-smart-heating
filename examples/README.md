# Safe examples

Run `python run_demo.py` from the repository root for a deterministic no-network simulation.

`data/sample_calendars/` contains synthetic scenarios: a normal stay, consecutive stays, blocked dates, cancellation before/after snapshots, and an imported channel feed. UIDs and summaries are invented fixtures. They are not reservation exports.

`.env.example` is the complete public configuration example. External integrations are blank. `demo_living_room` and `demo_bedroom` are local aliases, not real TCL devices. Do not substitute private values in example files or test fixtures.

For custom experiments, write a calendar under an ignored local location and inject `MockACController` and `StateStore(in_memory=True)`. Do not test by sending commands to an occupied property.

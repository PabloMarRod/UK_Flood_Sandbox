# Contributing

Thank you for helping. All kinds of contribution are welcome: bug reports, questions, data corrections, tests,
documentation and code.

## Reporting a problem
Open an issue with what you did, what you expected and what happened. Useful to include:
- the output of `python diagnose.py` (run from the app folder) if a download failed;
- the run folder's `params.json` and `model.rpt` if a run failed or looks wrong;
- your operating system and Python version.
Please do not post API keys or personal data.

## Suggesting a change
Open an issue first for anything substantial, so we can agree the approach. Then fork, make a branch, and open a
pull request that explains the change and how you tested it.

## Development
- Python 3.10 to 3.13. Create an environment and `pip install -r requirements.txt pytest`.
- Run the tests with `pytest` (the engine test runs a short storm, about half a minute).
- Keep the water balance exact: every new process must appear in the mass balance.
- Label assumptions in the app and in docs/technical-notes.md; prefer warnings to silent limits.
- Data sources: record their licence and attribution in NOTICE.

## Code of conduct
This project follows the [code of conduct](CODE_OF_CONDUCT.md).

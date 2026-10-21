# AI usage disclosure

Generative AI was used extensively in developing this software, and this file records how, as journals such as
the Journal of Open Source Software require.

## What was done with AI

* Most of the code, the documentation and the tests were drafted by Anthropic's Claude models through the
claude.ai chat interface (October 2026), working in a sandboxed computer environment, in a long, iterative
conversation with the author.
* The author set the goals and scope, chose between design options, decided which data sources and methods to
use, tested the app on his own computer, and reported problems and requests that drove each change.
* AI was also used to look up and read data-service documentation and the sources of published values (for
example the JRC depth-damage database).

## How correctness was checked

* Every run checks its water balance: all inflows, outflows and storage, including exchanges with tanks, pumps
and gates, must close (reported as the mass balance error, normally 0.000%).
* Estuary models were compared with tide gauge records, and four were calibrated and checked on a separate
period (see docs/technical-notes.md).
* Each feature was tested end to end in a headless browser and against reference runs during development, and
problems found that way were fixed and recorded (for example roof downpipes draining into enclosed courtyards,
flood gate links placed on the wall itself, and missing exchange terms in the water balance).
* Published values (hazard method, damage curves and maximum damage values) were taken from their sources, not
from model memory, and are cited.
* The automated tests in tests/ check the scenario checks, mesh edits for defences, green roof behaviour, damage
curves and a complete short engine run.

## Responsibility

The authors are responsible for the software and for reviewing it, and will answer questions about any part of it.
That review is in progress for this first release; please report anything that looks wrong.


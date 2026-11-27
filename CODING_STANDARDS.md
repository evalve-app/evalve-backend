# Coding standards

Rules for review. The tests cover behaviour and the OpenAPI file (`python -B -m unittest discover -s tests`, the Check workflow), so this file holds only judgement calls.

## Words

- Names in code, comments, reason texts and test names use the words of `GLOSSARY.md` in the repo `evalve-deploy` (Loop, Run, Replay, Loop task, Assignment, Drive, Refresh), and none of its "Avoid" words.
- A comment or a docstring is one or more full sentences that give the reason, not the action that the code already shows.

## Documents agree with the diff

- `README.md` describes each behaviour that the diff changes for a caller or for a person who operates the backend.
- The module docstring agrees with the diff when the diff changes a rule that the docstring states (for example the order of the collect in `steps.py`).

## Kitaru calls (`src/evalve_backend/kitaru_api.py`)

- One function for each Kitaru call. Other modules call these functions and never build a path.
- A function that loads a list uses `_list`, so it reads each page.
- A credential, a worker token and a request body never go into a log line or into an exception text.
- `tests/fake_kitaru.py` has each route that the diff adds, and it behaves as Kitaru does. A comment says which behaviour of Kitaru it copies.

## Loop steps and rules

- `loop_rules.py` decides, with pure functions. `steps.py` applies the decision and makes the calls.
- A step writes a fact to the store before the Kitaru call that depends on the fact.
- A text that a person reads (a `reason`, a `run_error`) is a constant or comes from one function.

## The store

- A change to the `Store` protocol changes `PostgresStore` and `tests/memory_store.py` in the same diff, with a test in `tests/test_store.py`.
- A change to the schema is a new version in `migrations.py`. A released version does not change.

## Tests

- A test of a Loop goes through the v1 routes with the real steps, the fake Kitaru and the memory store. It does not call `_Step` or patch a function of the backend.
- The name of a test is a sentence that states one behaviour.
- A test compares a text that a person reads with a literal. It does not import the constant from the code.
- A test that says "no call" counts the requests of the fake Kitaru.

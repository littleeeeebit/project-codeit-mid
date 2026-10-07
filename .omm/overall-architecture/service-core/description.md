`service/service.py` is the only module api.py imports, and every screen action and policy decision lives here (see the comment above `app_resources`). It has three runtime roles:
- The process owner, `Resources`.
- The request pipeline: `ask` -> `submit_answer` -> `RequestRunner` -> `run_queued` -> `_execute_traced`.
- Thin wrappers that delegate Verify and dataset work to the evaluation, gold, drafting, answers, judges and maintenance modules. Paid ones start as daemon threads registered in `Resources._jobs`.
Authorization is `_authorize(principal, *capabilities)`. Every signed-in member is a `visitor` with all capabilities, so capabilities only narrow in-process callers such as the CLI and tests.
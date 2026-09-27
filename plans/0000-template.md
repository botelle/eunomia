---
id: 0000-template
status: draft
repo: operator/<repo>
zone: public
tier: 1
paths: []
depends_on: []
---

# <Feature name>

## 1. Goal
<One sentence: what is true when this is done that isn't true now.>

## 2. Deliverables
- <file/behaviour, concrete enough that "done" is not a judgement call>

## 3. Boundaries
<What NOT to do, and WHY — in the specific terms a session would otherwise get wrong.
Each entry: the claim, then the reasoning, because the reasoning is what transfers.
An empty section means this plan has not been interrogated yet. Ask: what would a
competent session, reading only this repo, reasonably conclude that is wrong here?>

- <don't X, because Y — where Y names the rule, not just the exception>

## 4. Definition of done
- <verifiable, runnable by the session itself; name the properties the tests prove>

## 5. Handoff
<What to record when finished: decisions taken and their boundaries, for whoever picks
up next. Not a changelog of files touched.>

## 6. Resources
- lease: <branch / path globs / sims / service>

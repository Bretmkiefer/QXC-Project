# AI assistant: plan and first scaffold

Goal: an assistant that is an expert on this database, can pull data, and
explains and recommends based on the client's feedback; later it can also
act in the app. It talks to a model through OpenRouter using an API key you
add later.

## What exists today

`app/assistant.py` plus `POST /api/assistant`. It is **off**: the endpoint
returns 404 unless `QXC_ASSISTANT_ENABLED=1` **and** `OPENROUTER_API_KEY` are
set, and `QXC_ASSISTANT_MODEL` names the OpenRouter model. It is read-only:
the model can call three lookup tools (search records, summarize by a field,
get a contract vehicle) and nothing else. There is no screen for it yet.
The tools and the chat loop are tested with a fake model; it has never been
run against OpenRouter.

## Where the API key goes

- **Not** in the repo, the database or the browser. Add it as a Secret
  Manager secret, mapped to the `OPENROUTER_API_KEY` environment variable on
  the Cloud Run service, the same way the login secret already is.
- Entering a key from a settings page is possible later but needs the app to
  write to Secret Manager, which gives the running service more power than it
  has today. Recommended first version: the key is set by whoever deploys.
- Set a spending limit on the OpenRouter key itself.

## Build order

1. **Try it for real (small).** Add the secret and model env vars, turn the
   flag on in a test deployment, and call the endpoint with a few questions.
   Fix the system prompt and tools from what it gets wrong.
2. **A chat panel** on the site (edit and view users) that calls the endpoint.
   Note the view-only role can't POST today, so it would need an exception.
3. **Client feedback capture.** Store feedback (who, text, linked record or
   filters) in a new Firestore collection. Give the assistant a
   `list_feedback` tool and a deterministic scoring tool for "which
   offices/vehicles/records match this feedback", so recommendations come
   from the data and the model only explains them.
4. **Acting in the app (write tools).** Only after 1 to 3 are solid. The
   model may *propose* an action (add a note, link a contact, save
   enrichment, tag a record); the user sees it and confirms with a button;
   only then does the server perform it. Edit role only, and every action is
   written to an audit log collection.
5. **Limits and monitoring:** per-user rate limit, a monthly spend cap,
   logging of tool calls (not full record text), and a kill switch (the flag).

## Rules the design already follows

- **Never reveals sources.** The tools return no source fields, and the system
  prompt tells the model not to guess where data came from.
- **Tool results are data, not instructions.** Record descriptions and company
  names are untrusted text; a description saying "ignore your rules" must not
  work. The prompt says so, the model can only call a fixed tool list, and
  (step 4) any write needs a human click.
- **Browser-supplied chat history is sanitized:** only plain user/assistant
  text is accepted, so a user can't smuggle in a forged system or tool message.
- **Bounded:** at most 5 tool rounds per question, 20 rows per result, 2,000
  characters per question.
- **What leaves the system:** only the question and the tool results the
  model asked for go to OpenRouter and the model provider. Contacts (names,
  emails, phones) are not exposed by any tool today; decide deliberately
  before adding one.

## Open decisions

- Which model(s) to allow, and the monthly budget.
- Whether the client should see the assistant, or only your team at first.
- Whether to log full conversations (useful for improving it, but they may
  contain client feedback you'd rather not keep).

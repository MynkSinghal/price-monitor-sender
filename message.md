Subject: Price capture feed — what we'll be sending you and how to use it

Hi team,

Quick heads-up on the price-completion feed we'll be POSTing to your endpoint, and what we need from you. There's nothing complicated here — but there are a couple of moving pieces (two senders, full snapshot every minute) that are easier to explain up front than to debug later.

---

## What we are doing

We have a small service running on each price-server box. It watches the price-group folders on that box. When a RANTask job finishes, it drops a `success.txt` file in its folder — that's our cue that the job is done. We pick up the file's creation timestamp.

Once every minute, the service builds a CSV of **every active price group** and POSTs it to your endpoint. That's the whole job — watch, build, POST, repeat.

We have **two of these services**, one in the UK and one in the US, both POSTing to the **same** URL you give us. They run independently. One does not know about the other.

- **UKPROD** — runs in London. Sees the `success.txt` files for the UK-side jobs (the vast majority of price groups).
- **USPROD** — runs in the US. Sees the `success.txt` files for the eleven US-side jobs: `CME_SPAN2A`, `CME_SPAN2I`, `CME_SPANE`, `CME_SPN_AI`, `CME_SPN_BE`, `CME_SPAN2S`, `OCC_CPM`, `OCCP`, `OCCP_NON`, `OCCS`, `OCCSSTD`.

You can tell which sender a POST came from by an HTTP header we set on every request:

    X-Sender-Site:  UKPROD       (or USPROD)
    X-Sender-Host:  ukvinpsp-004 (or whichever box actually sent it)
    X-Sender-Env:   PROD         (or DR / UAT / DEV during testing)

---

## What the CSV looks like

Plain text body. One line per active price group. Pipe `|` as the delimiter. No header row. Timestamps are the sender's local time, with seconds.

A typical row when we have the job:

    PATH|23/04/2026 17:05:33

A typical row when we don't have it yet (job hasn't run today, or it's a job that lives on the **other** sender, or it's a row we never get a `success.txt` for at all — see below):

    PATH|

That's it — just the price group name, a pipe, and either a timestamp or nothing.

The body is small: roughly 10–20 KB per call, around 187 rows.

---

## The thing to understand: every CSV is a FULL snapshot

This is the most important thing.

**Each POST contains every active price group, every time** — not just the ones that completed since the last POST. If something's done, the row has a timestamp. If it's not done yet, the row is still there, just with the timestamp blank.

This means:

- You don't need to remember anything between POSTs. Each one is the latest complete picture from that sender.
- If your endpoint is down for ten minutes, no big deal — when you come back, the next POST has the current state of everything. We don't queue anything; we just re-send the snapshot.
- The same row will appear in every POST all day. The only thing that changes is whether the timestamp column is empty or filled.

---

## How to merge UKPROD and USPROD

Because there are two senders and they run independently, you'll get **two separate POSTs** — one from UKPROD, one from USPROD. They might land at the same time, in any order, sometimes a second apart, sometimes minutes apart. That's all fine.

Both POSTs contain the **same row set in the same order**. The only difference is which side filled in the timestamps:

- The UKPROD POST has timestamps on UK-side rows. The eleven US-side rows are blank in it.
- The USPROD POST has timestamps on those eleven US-side rows. Everything else is blank.

What you do on your side:

1. **Keep the latest CSV from each sender separately.** Use the `X-Sender-Site` header as the key. Every fresh POST from UKPROD replaces your previous UKPROD copy. Same for USPROD.
2. **When you build the merged view, walk row-by-row and pick whichever side has the non-empty timestamp.** If both sides happen to have the same row filled (rare — only happens if a job runs on both boxes), take the later of the two timestamps.
3. **Empty on both sides means the job hasn't run today on either box yet.** Show the row as pending in your UI; the next cycle from whichever sender will fill it in.

There's no race condition to worry about. Order of arrival doesn't matter. If UKPROD POST arrives before USPROD or after, you end up with the same merged view either way.

---

## A few special cases worth flagging

**1. Some rows will always be empty in our CSV — by design.**

A handful of price groups don't have a RANTask job on either box (manual processing, KSE clients where the file is picked up directly by the MM team, a couple of legacy rows that come from a different system). We still include them in the CSV every cycle so you can see they exist, but the timestamp column will always be blank from us. **You'll need to fill those in by hand or from another source.** Examples: `RCFT`, `PCFF`, `PDCE`, `Citadel KSE`, `KSE_PP15 / KRXK (HSBC)`, `Macquarie KSE (MB1)`, `STONEX KSE`, `Marex KSE`, `Jump Trading KSE`, `JPM_KSE`, `PSTM / SSTM / POMT / SOMT`, `ISTM`. There are about a dozen of these.

**2. `JSE1/JSE` is an "either/or" row.**

Some days we get only the `JSE` file. Some days we get only `JSE1`. Some days we get both. Whichever shows up first flags the row. If both show up, the row carries the later of the two timestamps. You'll just see one row labelled `JSE1/JSE` with a timestamp — same as any other row.

**3. `TASE` and `TASE_F` are split by weekday.**

`TASE` flags Monday through Thursday. `TASE_F` flags on Friday. They appear as two separate rows in our CSV every day. On a Friday, `TASE` will be blank and `TASE_F` will be filled. The rest of the week, the opposite.

**4. We POST Monday through Friday only.** No traffic on weekends.

**5. We POST every 60 seconds. About 1,400 calls per business day, per sender.**

---

## What we need a 200 back

We treat any HTTP 200 response as success. Anything else (4xx, 5xx, timeout, refused) we log as a failure and the next cycle re-sends the same snapshot. So a transient hiccup on your side self-heals on its own — you don't have to do anything.

Body of the response can be empty. We don't read it.

**One small ask:** please don't reject a POST just because the body is short or the timestamp column is empty for everything. At the very start of the day, before any job has run, our POST will literally be 187 rows of `name|` with nothing after the pipe. That's a valid, healthy POST.

---

## What we need from you

1. **The endpoint URL** to POST to. Any URL — `http://host:port/whatever` or HTTPS — both work for us. It doesn't have to be `/prices` or any particular path.
2. **Auth**, if you want any. Default is none. If you'd like a bearer token or an API key in a header, tell us the header name and value and we'll add it.
3. **Empty-cell preference.** Right now we send a literal blank after the pipe (`PATH|` with nothing). If you'd rather see a sentinel like `PATH|pending` or `PATH|NA` or `PATH|-`, just tell us — it's a one-line config change on our side, no code rebuild.
4. **Anything else on your side we should know about** — rate limits, max body size, IP allowlist, content-type preference, retry semantics. We currently send `Content-Type: text/csv`.

Happy to jump on a call if that's easier than email. Once we have the URL and any auth, we can be live in a few hours on each box.

Thanks!

— Price Support

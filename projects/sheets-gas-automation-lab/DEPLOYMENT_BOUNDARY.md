# Deployment boundary

This package does not call Google. Nothing here was measured against a live Apps Script project, a spreadsheet, or remaining quota. The sentences below are the public rules the example is written against. They can change, and this file is not a capacity plan.

Source pages:

- Quotas for Google Services: https://developers.google.com/apps-script/guides/services/quotas
- Installable Triggers: https://developers.google.com/apps-script/guides/triggers/installable
- Class Lock: https://developers.google.com/apps-script/reference/lock/lock

## Quotas copied for the boundary

Quotas are per user and reset 24 hours after the first request.

All quotas are subject to elimination, reduction, or change at any time, without notice.

| Feature | Consumer accounts (for example, gmail.com) | Google Workspace accounts |
| --- | --- | --- |
| Script runtime | 6 min / execution | 6 min / execution |
| Triggers total runtime | 90 min / day | 6 hr / day |
| Triggers | 20 / user / script | 20 / user / script |
| Properties value size | 9 KB / val | 9 KB / val |
| Properties total storage | 500 KB / property store | 500 KB / property store |

The operation ledger is a sheet column because a properties value is limited to 9 KB and the store to 500 KB. The Python core does the fee arithmetic so the per-execution runtime limit is not where the total is defined. This lab does not assert that headroom remains.

## Triggers

Installable triggers always run under the account of the person who created them.

Script executions and API requests don't cause triggers to run.

The time might be slightly randomized. A clock trigger requested for a given hour is not a deterministic timestamp, so this lab does not install one and does not treat `onEdit` as the write gate.

When an installable trigger fails, no error message appears on your screen. A retry has to learn what committed by reading the operation-id column, not by assuming the caller saw an error.

## What the example is allowed to do

`gas/Code.gs` takes `LockService.getScriptLock()`, uses `tryLock`, writes nothing when that returns false, and calls `SpreadsheetApp.flush()` before `releaseLock()`. It does not install a trigger, call UrlFetch, or store the ledger in Script Properties.

/**
 * Session-fee commit example for a container or standalone Apps Script project.
 *
 * The Python tests read this file as text. They do not execute it, install a
 * trigger, or call Google. There is no UrlFetch and no Script Properties
 * ledger: the operation id column on SessionLedger is the record of what
 * already committed.
 *
 * Outcome names match the Python ledger. The numbers in comments are the
 * draft's HTTP analogues, not a claim that the sheet speaks HTTP:
 *   missing_key            comment 400
 *   payload_conflict       comment 422
 *   in_progress_conflict   comment 409
 *   replay                 stored row
 *
 * Quotas and trigger behavior are documented in DEPLOYMENT_BOUNDARY.md.
 * This example does not install a clock trigger. onEdit is not the write gate:
 * script executions and API requests do not run triggers.
 */
var LEDGER_SHEET = 'SessionLedger';
var LOCK_WAIT_MS = 10000;

function commitSessionFee(operationId, canonicalRow) {
  if (!operationId) {
    return { outcome: 'missing_key', httpComment: 400, wrote: false };
  }
  var lock = LockService.getScriptLock();
  var acquired = lock.tryLock(LOCK_WAIT_MS);
  if (!acquired) {
    return { outcome: 'not_acquired', wrote: false };
  }
  try {
    var sheet = SpreadsheetApp.getActive().getSheetByName(LEDGER_SHEET);
    // Re-read the operation-id column only after the lock is held.
    var existing = readOperationRow_(sheet, operationId);
    if (existing) {
      if (existing.fingerprint !== canonicalRow.fingerprint) {
        return { outcome: 'payload_conflict', httpComment: 422, wrote: false };
      }
      return { outcome: 'replay', httpComment: 200, wrote: false, row: existing };
    }
    writeCanonicalRow_(sheet, canonicalRow);
    SpreadsheetApp.flush();
    return { outcome: 'applied', wrote: true };
  } finally {
    SpreadsheetApp.flush();
    lock.releaseLock();
  }
}

function readOperationRow_(sheet, operationId) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return null;
  }
  var numRows = lastRow - 1;
  var values = sheet.getRange(2, 1, numRows, 7).getValues();
  for (var index = 0; index < values.length; index++) {
    if (values[index][0] === operationId) {
      return {
        operationId: values[index][0],
        fingerprint: values[index][6],
        rowNumber: index + 2
      };
    }
  }
  return null;
}

function writeCanonicalRow_(sheet, canonicalRow) {
  // SpreadsheetApp's setValues and appendRow take no valueInputOption and may
  // parse strings the way the UI does, so '2026-10-05' could become a date
  // serial. This behavior was not observed here; the file is not run. The
  // text columns (operation id, ISO date, desk code, fingerprint) get the
  // plain-text format '@' before the write. hours, rate, and amount arrive
  // as JavaScript numbers that the Python core already checked.
  // canonicalRow.fingerprint is the Python core's pre-coercion SHA-256.
  var rowNumber = sheet.getLastRow() + 1;
  sheet.getRange(rowNumber, 1, 1, 3).setNumberFormat('@');
  sheet.getRange(rowNumber, 7).setNumberFormat('@');
  sheet.getRange(rowNumber, 1, 1, 7).setValues([[
    canonicalRow.operationId,
    canonicalRow.sessionDate,
    canonicalRow.deskCode,
    canonicalRow.hours,
    canonicalRow.rateJpyPerHour,
    canonicalRow.amountJpy,
    canonicalRow.fingerprint
  ]]);
}

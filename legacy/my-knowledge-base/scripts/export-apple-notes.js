#!/usr/bin/env osascript -l JavaScript

/** On-demand, read-only note HTML export. Attachment bytes are not exported. */
function collectNotes(accounts) {
  const notes = [];
  const report = {
    source: 'apple-notes', status: 'complete', discoveredNotes: 0,
    exportedNotes: 0, failedNotes: 0, failedAccounts: 0,
    attachmentBytesCaptured: false, receipts: []
  };
  for (let accountIndex = 0; accountIndex < accounts.length; accountIndex++) {
    const account = accounts[accountIndex];
    let accountName = 'account-' + accountIndex;
    let accountNotes;
    try {
      accountName = account.name();
      accountNotes = account.notes();
    } catch (e) {
      report.failedAccounts++;
      report.receipts.push({ id: accountName, stage: 'list', status: 'failed', reason: 'account-read-failed' });
      continue;
    }
    report.discoveredNotes += accountNotes.length;
    for (let i = 0; i < accountNotes.length; i++) {
      let id = accountName + ':note-index-' + i;
      try {
        const note = accountNotes[i];
        id = note.id();
        const title = note.name();
        const htmlBody = note.body();
        const created = note.creationDate();
        const modified = note.modificationDate();
        if (typeof id !== 'string' || !id || typeof htmlBody !== 'string') throw new Error('Invalid note');
        notes.push({ id, account: accountName, title, htmlBody,
          created: created ? created.toISOString() : null,
          modified: modified ? modified.toISOString() : null });
        report.exportedNotes++;
        report.receipts.push({ id, stage: 'export', status: 'captured', limitations: ['attachment-bytes-not-captured'] });
      } catch (e) {
        report.failedNotes++;
        report.receipts.push({ id, stage: 'export', status: 'failed', reason: 'note-read-failed' });
      }
    }
  }
  if (report.failedNotes || report.failedAccounts) report.status = notes.length ? 'partial' : 'failed';
  return { schemaVersion: 1, notes, report };
}

function run(argv) {
  try {
    return JSON.stringify(collectNotes(Application('Notes').accounts()), null, 2);
  } catch (e) {
    return JSON.stringify({ schemaVersion: 1, notes: [], report: {
      source: 'apple-notes', status: 'failed', discoveredNotes: 0, exportedNotes: 0,
      failedNotes: 0, failedAccounts: 0, attachmentBytesCaptured: false,
      receipts: [{ id: 'apple-notes', stage: 'list', status: 'failed', reason: 'notes-unavailable' }]
    } }, null, 2);
  }
}

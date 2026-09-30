function writeNormalizedRows(rows) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName('Data');
  if (!sheet) sheet = ss.insertSheet('Data');

  const headers = [['id', 'category', 'price', 'stock']];
  sheet.clearContents();
  sheet.getRange(1, 1, 1, headers[0].length).setValues(headers);

  if (rows.length > 0) {
    const values = rows.map(r => [r.id, r.category, r.price, r.stock]);
    sheet.getRange(2, 1, values.length, headers[0].length).setValues(values);
  }
}

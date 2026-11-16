import { expect } from '@playwright/test';

/** The DOCX Edits tab is the page editor: wait for it, then change the n-th editable text (document order) by appending `suffix`.
 *  Returns the exact original text so callers can verify the saved file independently. */
export async function openDocxEditor(page) {
  await expect(page.getByTestId('docx-page-editor')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('target-list').locator('button').first()).toBeVisible({ timeout: 60_000 });
}

export async function appendToDocxText(page, index, suffix) {
  await openDocxEditor(page);
  await page.getByTestId('target-list').locator('button').nth(index).click();
  const oldText = await page.getByTestId('original-value').evaluate((el) => el.textContent);
  await page.getByTestId('replacement').fill(oldText + suffix);
  await expect(page.locator('#edit-state')).toHaveText('Unsaved changes');
  return oldText;
}

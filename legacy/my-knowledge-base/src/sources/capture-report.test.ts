import { describe, expect, it } from 'vitest';
import { captureReport } from './capture-report.js';

describe('capture report classification', () => {
  it('does not count an incomplete listing as a captured body', () => {
    expect(captureReport('synthetic', [{ id: 'root', stage: 'list', status: 'incomplete', reason: 'incomplete-search' }]).status).toBe('failed');
  });
});

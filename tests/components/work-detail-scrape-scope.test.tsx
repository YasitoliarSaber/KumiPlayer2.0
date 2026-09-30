import { beforeEach, expect, test, vi } from 'vitest';

const api = vi.hoisted(() => ({
  metadataSearch: vi.fn(),
  metadataConfirm: vi.fn(),
}));
vi.mock('../../src/api/mediaV4', () => ({ mediaV4Api: api }));

import { workDetailV4Compatibility } from '../../src/api/workDetailV4Compatibility';

beforeEach(() => {
  api.metadataSearch.mockReset();
  api.metadataConfirm.mockReset();
  api.metadataSearch.mockResolvedValue({ candidates: [{
    candidate_id: 'candidate-1', provider_id: '123', media_type: 'tv',
    title: 'Online show', original_title: '', year: null,
  }] });
  api.metadataConfirm.mockResolvedValue({ status: 'confirmed', scope: 'season', season_number: 2,
    episode_mapping_status: 'partial', mapped_count: 1, total_count: 2, refresh_status: 'partial' });
});

test('selected season reaches V4 confirmation and returns the real mapping result', async () => {
  await workDetailV4Compatibility.scrape.searchCandidates('work-1', 'Online show', 2020);
  const task = await workDetailV4Compatibility.scrape.selectCandidate(
    'work-1', 123, 'tv', 'manual_replace', 'Online show', 2, true, 'work-1', 'season',
  );
  expect(api.metadataConfirm).toHaveBeenCalledWith({
    work_id: 'work-1', candidate_id: 'candidate-1', scope: 'season', season_number: 2,
  });
  expect(task.result.mapped_count).toBe(1);
  expect(task.result.total_count).toBe(2);
});

test('a season request without a selected regular season never refreshes the whole work', async () => {
  await workDetailV4Compatibility.scrape.searchCandidates('work-2', 'Online show');
  await expect(workDetailV4Compatibility.scrape.selectCandidate(
    'work-2', 123, 'tv', 'manual_replace', 'Online show', undefined, true, 'work-2', 'season',
  )).rejects.toThrow('正片季度');
  expect(api.metadataConfirm).not.toHaveBeenCalled();
});

test('an unavailable provider result is not presented as a completed scrape', async () => {
  await workDetailV4Compatibility.scrape.searchCandidates('work-3', 'Online show');
  api.metadataConfirm.mockResolvedValue({ status: 'refresh_unavailable' });
  await expect(workDetailV4Compatibility.scrape.selectCandidate(
    'work-3', 123, 'tv', 'manual_replace', 'Online show', 1, true, 'work-3', 'work',
  )).rejects.toThrow('刷新未完成');
});

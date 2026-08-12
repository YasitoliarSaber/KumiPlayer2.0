import test from 'node:test';
import assert from 'node:assert/strict';
import type { ScrapeCandidate } from '../src/api/scrape.ts';
import {
  candidateDisplayTitles,
  formatCandidateScore,
} from '../src/utils/scrapeCandidate.ts';

function candidate(overrides: Partial<ScrapeCandidate>): ScrapeCandidate {
  return {
    candidate_id: 'candidate', scrape_target_id: 'target', provider: 'anilist',
    tmdb_id: 1, tmdb_type: 'movie', title: '', original_title: '', year: 2007,
    overview: '', poster_path: '', popularity: 0, vote_average: 0, score: 0,
    reasons: [], ...overrides,
  };
}

test('动画候选优先显示中文主标题和日文副标题', () => {
  const result = candidateDisplayTitles('福音战士新剧场版：序', candidate({
    title: 'Evangelion Shin Movie: Jo',
    original_title: 'ヱヴァンゲリヲン新劇場版:序',
    raw: {
      provider_title_aliases: ['福音戰士新劇場版：序', 'Evangelion Shin Movie: Jo'],
    },
  }));

  assert.equal(result.primary, '福音战士新剧场版：序');
  assert.equal(result.secondary, 'ヱヴァンゲリヲン新劇場版:序');
});

test('跨语言剧场版候选可沿用已匹配的本地中文标题', () => {
  const result = candidateDisplayTitles('CLANNAD 剧场版', candidate({
    title: 'Clannad Movie',
    original_title: '劇場版 クラナド',
    raw: {
      provider_title_aliases: ['劇場版 クラナド', 'Clannad: The Motion Picture', 'Clannad Movie'],
    },
  }));

  assert.equal(result.primary, 'CLANNAD 剧场版');
  assert.equal(result.secondary, '劇場版 クラナド');
});

test('候选匹配分不重复乘以一百', () => {
  assert.equal(formatCandidateScore(122), '122 分');
  assert.equal(formatCandidateScore(103), '103 分');
});

import type { WorkIndex } from '../api/types'

export function mainEpisodeCount(work: WorkIndex) {
  const direct = Number((work as WorkIndex & { main_episode_count?: number }).main_episode_count ?? 0)
  if (direct > 0) return direct
  const seasonCount = (work.seasons ?? [])
    .filter((season) => (season.group_type || 'season') === 'season')
    .reduce((sum, season) => sum + Number(season.episode_count || 0), 0)
  if (seasonCount > 0) return seasonCount
  return (work.episodes ?? []).filter((episode) => episode.group_type === 'season').length
}

export function totalEpisodeCount(work: WorkIndex) {
  const direct = Number((work as WorkIndex & { episode_count?: number }).episode_count ?? 0)
  return direct > 0 ? direct : (work.episodes?.length ?? 0)
}

import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, CheckCircle2, FolderOpen, Heart, Play, RefreshCw } from 'lucide-react';
import { useLibraryStore } from '../stores/library';
import { useUiStore } from '../stores/ui';
import { libraryApi } from '../api/library';
import { playbackApi } from '../api/playback';
import { systemApi } from '../api/system';
import { buildAssetUrl } from '../api/assets';
import LoadingState from '../components/ui/loading-state';
import DecodedImage from '../components/ui/DecodedImage';

type WorkDetail = any;

function seasonKey(season: any) {
  return `${season?.group_type || 'season'}:${Number(season?.season_number ?? 0)}`;
}

function episodeLabel(episode: any) {
  if (episode.special_number != null) return `特别篇 ${episode.special_number}`;
  if (episode.episode_number == null) return '未编号';
  return `第 ${episode.episode_number} 集`;
}

function sourceLabel(source: string) {
  return source === 'pan115' ? '115' : source === 'baidu' ? '百度' : source === 'openlist' ? 'OpenList' : '本地';
}

export default function WorkDetailPage() {
  const selectedWorkId = useUiStore((state) => state.selectedWorkId);
  const activeCategory = useUiStore((state) => state.activeCategory);
  const selectedSeasonNumber = useUiStore((state) => state.selectedSeasonNumber);
  const selectSeason = useUiStore((state) => state.selectSeason);
  const rememberWorkSeason = useUiStore((state) => state.rememberWorkSeason);
  const goCategory = useUiStore((state) => state.goCategory);
  const getWorkDetail = useLibraryStore((state) => state.getWorkDetail);
  const updateWorkWatchStatus = useLibraryStore((state) => state.updateWorkWatchStatus);
  const [work, setWork] = useState<WorkDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selectedSeasonKey, setSelectedSeasonKey] = useState('');
  const [busyEpisodeId, setBusyEpisodeId] = useState('');

  const loadWork = async () => {
    if (!selectedWorkId) return;
    setLoading(true);
    setError('');
    try {
      const next = await getWorkDetail(selectedWorkId);
      setWork(next);
      const seasons = next.seasons || [];
      const preferred = seasons.find((season: any) => Number(season.season_number) === selectedSeasonNumber) || seasons[0];
      setSelectedSeasonKey(preferred ? seasonKey(preferred) : '');
      if (preferred) selectSeason(Number(preferred.season_number));
    } catch (loadError) {
      setError((loadError as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadWork();
  }, [selectedWorkId]);

  const seasons = work?.seasons || [];
  const currentSeason = seasons.find((season: any) => seasonKey(season) === selectedSeasonKey) || seasons[0] || null;
  const episodes = useMemo(() => {
    const all = work?.episodes || [];
    if (!currentSeason) return all;
    return all.filter((episode: any) => Number(episode.season_number) === Number(currentSeason.season_number));
  }, [currentSeason, work?.episodes]);

  const favorite = Boolean(work?.watch_status?.favorite);

  const selectDetailSeason = (season: any) => {
    const key = seasonKey(season);
    setSelectedSeasonKey(key);
    selectSeason(Number(season.season_number));
    if (work?.work_id) {
      rememberWorkSeason(work.work_id, {
        seasonNumber: Number(season.season_number),
        seasonKey: key,
      });
    }
  };

  const toggleFavorite = async () => {
    if (!work) return;
    const nextFavorite = !favorite;
    try {
      const current = work.watch_status || { status: '', note: '' };
      const updated = await libraryApi.setWatchStatus(work.work_id, current.status || '', current.note || '', nextFavorite);
      const next = { ...work, watch_status: updated };
      setWork(next);
      updateWorkWatchStatus(work.work_id, updated);
      setNotice(nextFavorite ? '已收藏' : '已取消收藏');
    } catch (toggleError) {
      setNotice((toggleError as Error).message);
    }
  };

  const playEpisode = async (episode: any) => {
    if (!work || !episode?.episode_id) return;
    setBusyEpisodeId(episode.episode_id);
    try {
      await playbackApi.play({
        work_id: work.work_id,
        episode_id: episode.episode_id,
        asset_id: episode.asset_id || undefined,
      });
      setNotice(`${episodeLabel(episode)} 已交给播放器`);
    } catch (playError) {
      setNotice((playError as Error).message);
    } finally {
      setBusyEpisodeId('');
    }
  };

  const markEpisode = async (episode: any, completed: boolean) => {
    if (!work || !episode?.episode_id) return;
    try {
      await playbackApi.markProgress({
        work_id: work.work_id,
        episode_id: episode.episode_id,
        asset_id: episode.asset_id || undefined,
        completed,
      });
      setNotice(completed ? `${episodeLabel(episode)} 已标记为看过` : `${episodeLabel(episode)} 已恢复未看`);
    } catch (markError) {
      setNotice((markError as Error).message);
    }
  };

  const openFolder = async (episode: any) => {
    if (!work) return;
    try {
      const result = await systemApi.openFolder(work.work_id, episode?.episode_id || '');
      setNotice(result.exists ? '已打开来源目录' : '来源目录不可访问');
    } catch (openError) {
      setNotice((openError as Error).message);
    }
  };

  if (loading) return <LoadingState label="正在载入作品" detail="正在读取 V4 媒体库读模型" />;
  if (error) return <div className="page-loading-wrap"><div className="page-loading-message">{error}</div></div>;
  if (!work) return <div className="page-loading-wrap"><div className="page-loading-message">作品不存在</div></div>;

  const poster = work.poster_path ? buildAssetUrl(work.poster_path, { kind: 'poster' }) : '';
  const isMovie = work.media_type === 'movie';

  return (
    <div className="work-detail-page">
      <div className="work-detail-toolbar">
        <button type="button" onClick={() => goCategory(activeCategory || 'anime_series')}><ArrowLeft size={17} />返回媒体库</button>
        <div className="work-detail-toolbar-actions">
          <button type="button" onClick={() => void loadWork()} title="刷新作品"><RefreshCw size={16} />刷新</button>
          <button type="button" onClick={() => void toggleFavorite()} aria-pressed={favorite} title={favorite ? '取消收藏' : '收藏'}>
            <Heart size={16} fill={favorite ? 'currentColor' : 'none'} />{favorite ? '已收藏' : '收藏'}
          </button>
        </div>
      </div>

      <section className="work-detail-summary">
        {poster ? <DecodedImage src={poster} alt="" className="work-detail-poster" /> : <div className="work-detail-poster-placeholder" />}
        <div className="work-detail-summary-copy">
          <p className="work-detail-eyebrow">{isMovie ? '电影' : '剧集'} · {work.source ? sourceLabel(work.source) : 'V4 媒体库'}</p>
          <h1>{work.title}</h1>
          <p>{work.original_title && work.original_title !== work.title ? work.original_title : '已由 V4 统一作品图整理'}</p>
          <div className="work-detail-facts">
            {work.year && <span>{work.year}</span>}
            <span>{work.episode_count || work.episodes?.length || 0} 集</span>
            <span>{work.asset_count || 0} 个版本</span>
          </div>
          {notice && <div className="work-detail-notice" role="status">{notice}</div>}
        </div>
      </section>

      {!isMovie && seasons.length > 0 && (
        <div className="work-detail-seasons" role="tablist" aria-label="季度">
          {seasons.map((season: any) => (
            <button
              type="button"
              key={seasonKey(season)}
              className={seasonKey(season) === seasonKey(currentSeason) ? 'active' : ''}
              onClick={() => selectDetailSeason(season)}
            >
              {season.label || `第 ${season.season_number} 季`}<span>{season.episode_count || season.episodes?.length || 0}</span>
            </button>
          ))}
        </div>
      )}

      <section className="work-detail-episodes">
        <header><div><h2>{isMovie ? '播放资源' : (currentSeason?.label || '剧集')}</h2><span>{episodes.length} 集 · 多版本以 Asset 保留</span></div></header>
        {episodes.length === 0 ? <p className="work-detail-empty">当前作品还没有可播放的剧集。</p> : (
          <div className="work-detail-episode-list">
            {episodes.map((episode: any) => (
              <article className="work-detail-episode" key={episode.episode_id}>
                <div className="work-detail-episode-index"><strong>{episodeLabel(episode)}</strong><span>{episode.title || '未命名剧集'}</span></div>
                <div className="work-detail-episode-meta"><span>{sourceLabel(episode.source || work.source || 'local')}</span><span>{episode.assets?.length || 1} 个 Asset</span></div>
                <div className="work-detail-episode-actions">
                  <button type="button" onClick={() => void markEpisode(episode, true)} title="标记看过"><CheckCircle2 size={16} /></button>
                  <button type="button" onClick={() => void openFolder(episode)} title="打开来源目录"><FolderOpen size={16} /></button>
                  <button type="button" className="primary" disabled={busyEpisodeId === episode.episode_id} onClick={() => void playEpisode(episode)}><Play size={16} />{busyEpisodeId === episode.episode_id ? '处理中' : '播放'}</button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

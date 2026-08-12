import { useEffect, useMemo, useState } from 'react';
import { useLibraryStore } from '../stores/library';
import { useUiStore, type CategoryKey } from '../stores/ui';
import PosterCard from '../components/library/PosterCard';
import LoadingState from '../components/ui/loading-state';
import type { WorkIndex } from '../api/types';
import { buildAssetUrl } from '../api/assets';
import { isWorkInLibraryView } from '../utils/libraryCategories';
import {
  buildHomeShowcase,
  selectHomeCategoryWorks,
  selectSessionShowcase,
} from '../utils/homeShowcase';
import { ArrowRight, ChevronLeft, ChevronRight, FolderPlus, LibraryBig } from 'lucide-react';
import DecodedImage from '../components/ui/DecodedImage';

const categories: { key: CategoryKey; label: string }[] = [
  { key: 'anime_series', label: '番剧' },
  { key: 'anime_movie', label: '动画电影' },
  { key: 'live_series', label: '剧集' },
  { key: 'live_movie', label: '电影' },
];
export default function HomePage() {
  const works = useLibraryStore((state) => state.works);
  const history = useLibraryStore((state) => state.history);
  const loading = useLibraryStore((state) => state.loading);
  const error = useLibraryStore((state) => state.error);
  const openWorkDetail = useLibraryStore((state) => state.openWorkDetail);
  const getWorkDetail = useLibraryStore((state) => state.getWorkDetail);
  const { goCategory, goManage, source, sort } = useUiStore();
  const [featureIndex, setFeatureIndex] = useState(0);
  const [visibleFeatureIndex, setVisibleFeatureIndex] = useState(0);
  const [decodedFeatureImages, setDecodedFeatureImages] = useState<ReadonlySet<string>>(() => new Set());
  const [featureTimerKey, setFeatureTimerKey] = useState(0);

  const displayWorks = useMemo(
    () => source === 'all' ? works : works.filter((work) => work.source === source),
    [source, works],
  );
  const homeDisplayWorks = useMemo(
    () => selectHomeCategoryWorks(displayWorks, history, sort),
    [displayWorks, history, sort],
  );
  const showcaseWorks = useMemo(
    () => selectSessionShowcase(
      source,
      homeDisplayWorks,
      8,
    ),
    [homeDisplayWorks, source],
  );
  const { featured: featuredWorks, side: sideFeatured } = useMemo(
    () => buildHomeShowcase(showcaseWorks, 5, 3),
    [showcaseWorks],
  );
  const normalizedFeatureIndex = featureIndex % Math.max(featuredWorks.length, 1);
  const normalizedVisibleFeatureIndex = visibleFeatureIndex % Math.max(featuredWorks.length, 1);
  const requestedFeature = featuredWorks[normalizedFeatureIndex];
  const requestedFeatureImage = requestedFeature ? featureImageUrl(requestedFeature) : '';

  useEffect(() => {
    if (featuredWorks.length === 0) {
      setFeatureIndex(0);
      setVisibleFeatureIndex(0);
      return;
    }
    setFeatureIndex((value) => value % featuredWorks.length);
    setVisibleFeatureIndex((value) => value % featuredWorks.length);
  }, [featuredWorks.length]);

  useEffect(() => {
    if (requestedFeatureImage && decodedFeatureImages.has(requestedFeatureImage)) {
      setVisibleFeatureIndex(normalizedFeatureIndex);
    }
  }, [decodedFeatureImages, normalizedFeatureIndex, requestedFeatureImage]);

  useEffect(() => {
    if (featuredWorks.length <= 1) return;
    const timer = window.setInterval(() => {
      setFeatureIndex((value) => randomNextIndex(value, featuredWorks.length));
    }, 4000);
    return () => window.clearInterval(timer);
  }, [featuredWorks.length, featureTimerKey]);

  const resetFeatureTimer = () => setFeatureTimerKey((value) => value + 1);
  const markFeatureReady = (index: number) => {
    const work = featuredWorks[index];
    if (!work) return;
    const image = featureImageUrl(work);
    setDecodedFeatureImages((current) => {
      if (current.has(image)) return current;
      return new Set(current).add(image);
    });
  };


  if (loading && works.length === 0) return <LoadingState label="正在载入媒体库" detail="正在整理你的作品" />;
  if (error && works.length === 0) return <CenteredMessage>{error}</CenteredMessage>;
  if (displayWorks.length === 0) {
    const libraryIsEmpty = works.length === 0;
    return (
      <div className="home-library-empty">
        <div className="home-library-empty-content">
          <span className="home-library-empty-mark" aria-hidden="true"><LibraryBig size={25} /></span>
          <h1>{libraryIsEmpty ? '还没有作品' : '当前来源没有作品'}</h1>
          <p>
            {libraryIsEmpty
              ? '添加本地目录或导入网盘目录树，整理完成后作品会显示在首页。'
              : '可以添加这个来源的媒体，或从顶部来源菜单切换到其他媒体库。'}
          </p>
          <button type="button" className="home-library-empty-action" onClick={goManage}>
            <FolderPlus size={18} />
            <span>添加媒体</span>
          </button>
        </div>
      </div>
    );
  }
  if (homeDisplayWorks.length === 0) {
    return (
      <div className="home-library-empty">
        <div className="home-library-empty-content">
          <span className="home-library-empty-mark" aria-hidden="true"><LibraryBig size={25} /></span>
          <h1>首页暂无可展示作品</h1>
          <p>当前作品尚未生成有效封面；分类页仍会保留这些作品，便于继续检查和整理。</p>
          <button type="button" className="home-library-empty-action" onClick={goManage}>
            <FolderPlus size={18} />
            <span>前往媒体管理</span>
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="home-page space-y-10">

      {featuredWorks.length > 0 && (
        <section className="home-showcase">
          <div className="home-feature">
          {featuredWorks.map((work, index) => {
            const image = featureImageUrl(work);
            const active = index === normalizedVisibleFeatureIndex;
            return (
              <button
                key={`${work.work_id}-${image}`}
                type="button"
                className={`home-feature-primary ${active ? 'is-active' : ''}`}
                aria-label={`打开${work.title}详情`}
                aria-hidden={!active}
                tabIndex={active ? 0 : -1}
                onClick={() => void openWorkDetail(work.work_id)}
                onPointerEnter={() => void getWorkDetail(work.work_id).catch(() => undefined)}
                onFocus={() => void getWorkDetail(work.work_id).catch(() => undefined)}
              >
                <DecodedImage
                  src={image}
                  alt=""
                  aria-hidden="true"
                  className="home-feature-bg"
                />
                <DecodedImage
                  src={image}
                  alt={work.title}
                  className="home-feature-art"
                  onDecoded={() => markFeatureReady(index)}
                />
                <div className="absolute inset-0 z-[2] bg-gradient-to-t from-black/70 via-black/20 to-transparent" />
                <div className="absolute bottom-0 left-0 z-[3] max-w-2xl p-8 text-white">
                  <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
                    {work.rating > 0 && <span className="rounded bg-white/18 px-2 py-1">★ {work.rating.toFixed(1)}</span>}
                    <span className="rounded bg-white/18 px-2 py-1">{metaLine(work)}</span>
                  </div>
                  <h2 className="text-4xl font-bold">{work.title}</h2>
                  <p className="mt-3 line-clamp-2 text-sm leading-6 text-white/86">
                    {work.plot || '从媒体库随机推荐一部作品，点击进入详情页继续观看或整理资料。'}
                  </p>
                </div>
              </button>
            );
          })}
          {featuredWorks.length > 1 && (
            <>
            <div className="home-feature-edge left">
              <FeatureButton label="上一部" side="left" onClick={() => { setFeatureIndex((value) => (value - 1 + featuredWorks.length) % featuredWorks.length); resetFeatureTimer(); }} />
            </div>
            <div className="home-feature-edge right">
              <FeatureButton label="下一部" side="right" onClick={() => { setFeatureIndex((value) => (value + 1) % featuredWorks.length); resetFeatureTimer(); }} />
            </div>
            <div className="home-feature-dots" aria-label="随机推荐切换">
              {featuredWorks.map((work, index) => {
                const active = index === normalizedVisibleFeatureIndex;
                return (
                  <button
                    key={work.work_id}
                    type="button"
                    className={active ? 'active' : ''}
                    title={work.title}
                    aria-label={`显示${work.title}`}
                    aria-current={active ? 'true' : undefined}
                    onClick={(event) => {
                      event.stopPropagation();
                      setFeatureIndex(index);
                      resetFeatureTimer();
                    }}
                  />
                );
              })}
            </div>
            </>
          )}
          </div>
          <div className="home-feature-side">
            {sideFeatured.map((work, index) => {
              const image = buildAssetUrl(work.fanart_path || work.poster_path, { kind: work.fanart_path ? 'backdrop' : 'poster' });
              return (
                <button
                  key={`${work.work_id}-${index}`}
                  type="button"
                  className={`home-feature-tile ${index === 0 ? 'wide' : 'small'}`}
                  aria-label={`打开${work.title}详情`}
                  onClick={() => void openWorkDetail(work.work_id)}
                  onPointerEnter={() => void getWorkDetail(work.work_id).catch(() => undefined)}
                  onFocus={() => void getWorkDetail(work.work_id).catch(() => undefined)}
                >
                  <DecodedImage src={image} alt={work.title} loading="lazy" />
                  <span className="home-feature-tile-shade" />
                  <span className="home-feature-tile-meta">
                    <strong>{work.title}</strong>
                    <small>{metaLine(work)}{work.rating > 0 ? ` \u00b7 \u2605 ${work.rating.toFixed(1)}` : ''}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      )}

      {categories.map((category) => {
        const allCategoryWorks = homeDisplayWorks.filter(
          (work) => isWorkInLibraryView(work, category.key),
        );
        const categoryWorks = allCategoryWorks.slice(0, 8);
        if (categoryWorks.length === 0) return null;

        return (
          <section className="home-category-section" key={category.key}>
            <SectionTitle
              title={category.label}
              count={allCategoryWorks.length}
              onClick={() => goCategory(category.key)}
            />
            <div className="grid grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-5 2xl:grid-cols-8">
              {categoryWorks.map((work) => (
                <PosterCard key={work.work_id} work={work} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function SectionTitle({ title, count, onClick }: { title: string; count?: number; onClick: () => void }) {
  return (
    <div className="home-category-head">
      <h2>
        {title}
        {typeof count === 'number' && (
          <span>
            共 {count} 部
          </span>
        )}
      </h2>
      <button type="button" onClick={onClick} className="home-category-link">
        <span>查看全部</span>
        <ArrowRight size={15} aria-hidden="true" />
      </button>
    </div>
  );
}

function FeatureButton({ label, side, onClick }: { label: string; side: 'left' | 'right'; onClick: () => void }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className={`home-feature-nav ${side}`}
    >
      {side === 'left' ? <ChevronLeft size={20} aria-hidden="true" /> : <ChevronRight size={20} aria-hidden="true" />}
    </button>
  );
}

function CenteredMessage({ children }: { children: string }) {
  return (
    <div className="page-loading-wrap">
      <div className="page-loading-message">{children}</div>
    </div>
  );
}

function metaLine(work: WorkIndex) {
  if (work.show_type === 'anime_series' || work.show_type === 'live_series') {
    return `共 ${work.seasons?.length || 1} 季`;
  }
  return work.year ? `${work.year}` : '媒体库作品';
}

function featureImageUrl(work: WorkIndex) {
  return buildAssetUrl(
    work.fanart_path || work.poster_path,
    { kind: work.fanart_path ? 'backdrop' : 'poster' },
  );
}

function randomNextIndex(current: number, total: number) {
  if (total <= 1) return 0;
  const normalized = current % total;
  let next = Math.floor(Math.random() * total);
  if (next === normalized) next = (next + 1) % total;
  return next;
}

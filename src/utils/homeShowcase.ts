import type { SortId } from '../stores/ui';

type ShowcaseWork = {
  work_id: string;
};

type HomeCategoryWork = ShowcaseWork & {
  title: string;
  poster_path: string;
  rating?: number;
  year?: number | null;
};

type PlaybackHistoryWork = {
  work_id: string;
};

type SessionShowcaseOrder = {
  signature: string;
  workIds: string[];
};

const sessionShowcaseOrders = new Map<string, SessionShowcaseOrder>();

export function selectSessionShowcase<T extends ShowcaseWork>(
  cacheKey: string,
  works: T[],
  limit = 8,
  random: () => number = Math.random,
): T[] {
  if (works.length === 0 || limit <= 0) return [];

  const normalizedLimit = Math.max(1, Math.floor(limit));
  const signature = `${normalizedLimit}\u001e${works.map((work) => work.work_id).sort().join('\u001f')}`;
  let order = sessionShowcaseOrders.get(cacheKey);

  if (!order || order.signature !== signature) {
    const shuffled = [...works];
    for (let index = shuffled.length - 1; index > 0; index -= 1) {
      const target = Math.floor(random() * (index + 1));
      [shuffled[index], shuffled[target]] = [shuffled[target], shuffled[index]];
    }
    order = {
      signature,
      workIds: shuffled.slice(0, normalizedLimit).map((work) => work.work_id),
    };
    sessionShowcaseOrders.set(cacheKey, order);
  }

  const workById = new Map(works.map((work) => [work.work_id, work]));
  return order.workIds.flatMap((workId) => {
    const work = workById.get(workId);
    return work ? [work] : [];
  });
}

export function buildHomeShowcase<T>(
  works: T[],
  featuredLimit = 5,
  sideSlotCount = 3,
): { featured: T[]; side: T[] } {
  if (works.length === 0) return { featured: [], side: [] };

  const normalizedFeaturedLimit = Math.max(1, Math.floor(featuredLimit));
  const normalizedSideSlotCount = Math.max(0, Math.floor(sideSlotCount));
  const reservedSideWorks = Math.min(normalizedSideSlotCount, Math.max(0, works.length - 1));
  const featuredCount = Math.min(normalizedFeaturedLimit, works.length - reservedSideWorks);
  const featured = works.slice(0, featuredCount);
  const sidePool = works.slice(featuredCount);
  const pool = sidePool.length > 0 ? sidePool : featured;
  const side = Array.from(
    { length: normalizedSideSlotCount },
    (_, index) => pool[index % pool.length],
  );

  return { featured, side };
}

export function selectHomeCategoryWorks<T extends HomeCategoryWork>(
  works: T[],
  history: PlaybackHistoryWork[],
  sort: SortId,
  limit = Number.POSITIVE_INFINITY,
): T[] {
  const visible = works.filter((work) => Boolean(work.poster_path?.trim()));
  const recentRank = new Map<string, number>();
  history.forEach((item, index) => {
    if (!recentRank.has(item.work_id)) recentRank.set(item.work_id, index);
  });

  visible.sort((left, right) => {
    let difference = 0;
    switch (sort) {
      case 'recent':
        difference = (recentRank.get(left.work_id) ?? Number.MAX_SAFE_INTEGER)
          - (recentRank.get(right.work_id) ?? Number.MAX_SAFE_INTEGER);
        break;
      case 'rating':
      case 'ratingDesc':
        difference = (right.rating || 0) - (left.rating || 0);
        break;
      case 'ratingAsc':
        difference = (left.rating || 0) - (right.rating || 0);
        break;
      case 'year':
      case 'yearDesc':
        difference = (right.year || 0) - (left.year || 0);
        break;
      case 'yearAsc':
        difference = (left.year ?? 9999) - (right.year ?? 9999);
        break;
      case 'title':
        break;
      case 'titleDesc':
        difference = right.title.localeCompare(left.title, 'zh-Hans-CN');
        break;
      default:
        return 0;
    }
    return difference || left.title.localeCompare(right.title, 'zh-Hans-CN');
  });

  return visible.slice(0, Math.max(0, Math.floor(limit)));
}

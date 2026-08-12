import type { SortId } from '../stores/ui';

export type SortDimension = 'recent' | 'title' | 'rating' | 'year';

export interface SortOption {
  dimension: SortDimension;
  label: string;
  directionLabel: string;
}

const sortOptions: Record<SortDimension, Omit<SortOption, 'directionLabel'>> = {
  recent: { dimension: 'recent', label: '最近播放' },
  title: { dimension: 'title', label: '名称' },
  rating: { dimension: 'rating', label: '评分' },
  year: { dimension: 'year', label: '年份' },
};

export const sortDimensions: SortDimension[] = ['recent', 'title', 'rating', 'year'];

export function getSortDimension(sort: SortId): SortDimension {
  if (sort === 'rating' || sort === 'ratingDesc' || sort === 'ratingAsc') return 'rating';
  if (sort === 'year' || sort === 'yearDesc' || sort === 'yearAsc') return 'year';
  if (sort === 'title' || sort === 'titleDesc') return 'title';
  return 'recent';
}

export function getSortOption(sort: SortId): SortOption {
  const dimension = getSortDimension(sort);
  const directionLabel = dimension === 'recent'
    ? ''
    : dimension === 'title'
      ? sort === 'titleDesc' ? 'Z-A' : 'A-Z'
      : dimension === 'rating'
        ? sort === 'ratingAsc' ? '低到高' : '高到低'
        : sort === 'yearAsc' ? '旧到新' : '新到旧';
  return { ...sortOptions[dimension], directionLabel };
}

export function toggleSort(current: SortId, dimension: SortDimension): SortId {
  if (dimension !== getSortDimension(current)) {
    return dimension === 'recent'
      ? 'recent'
      : dimension === 'title'
        ? 'title'
        : dimension === 'rating'
          ? 'ratingDesc'
          : 'yearDesc';
  }

  if (dimension === 'title') return current === 'titleDesc' ? 'title' : 'titleDesc';
  if (dimension === 'rating') return current === 'ratingAsc' ? 'ratingDesc' : 'ratingAsc';
  if (dimension === 'year') return current === 'yearAsc' ? 'yearDesc' : 'yearAsc';
  return 'recent';
}

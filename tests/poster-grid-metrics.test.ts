import test from 'node:test';
import assert from 'node:assert/strict';
import { calculatePosterGridMetrics } from '../src/components/library/posterGridMetrics.ts';

test('连续切换列数与容器宽度时始终得到有限且一致的封面网格尺寸', () => {
  const widths = [1960, 1280, 720, 1540, 930, 2048, 640];
  const requestedColumns = [5, 8, 3, 7, 4, 6, 8];

  widths.forEach((width, index) => {
    const metrics = calculatePosterGridMetrics({
      width,
      gap: 24,
      requestedColumns: requestedColumns[index],
      imageMode: 'poster',
      metaHeight: 62,
    });

    assert.ok(Number.isFinite(metrics.columnWidth));
    assert.ok(Number.isFinite(metrics.rowHeight));
    assert.ok(metrics.effectiveColumns >= 1);
    assert.ok(metrics.effectiveColumns <= requestedColumns[index]);
    assert.equal(metrics.rowHeight, metrics.columnWidth * 1.5 + 62 + 24);
  });
});

test('横向海报和竖版封面使用各自稳定比例且窄屏自动降列', () => {
  const poster = calculatePosterGridMetrics({
    width: 650,
    gap: 18,
    requestedColumns: 8,
    imageMode: 'poster',
    metaHeight: 62,
  });
  const fanart = calculatePosterGridMetrics({
    width: 650,
    gap: 18,
    requestedColumns: 8,
    imageMode: 'fanart',
    metaHeight: 62,
  });

  assert.equal(poster.effectiveColumns, 4);
  assert.equal(fanart.effectiveColumns, 3);
  assert.equal(poster.columnCapacity, 4);
  assert.equal(fanart.columnCapacity, 3);
  assert.equal(poster.rowHeight, poster.columnWidth * 1.5 + 62 + 18);
  assert.equal(fanart.rowHeight, fanart.columnWidth * 9 / 16 + 62 + 18);
});

test('窄窗口同时返回滑块可用列数上限且不覆盖宽屏偏好', () => {
  const preferredEightColumns = calculatePosterGridMetrics({
    width: 1030,
    gap: 20,
    requestedColumns: 8,
    imageMode: 'fanart',
    metaHeight: 62,
  });
  const selectedFiveColumns = calculatePosterGridMetrics({
    width: 1030,
    gap: 20,
    requestedColumns: 5,
    imageMode: 'fanart',
    metaHeight: 62,
  });

  assert.equal(preferredEightColumns.columnCapacity, 5);
  assert.equal(preferredEightColumns.effectiveColumns, 5);
  assert.equal(selectedFiveColumns.columnCapacity, 5);
  assert.equal(selectedFiveColumns.effectiveColumns, 5);
});

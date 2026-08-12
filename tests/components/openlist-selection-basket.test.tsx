// OLIST-02：OpenList 选择篮纯逻辑测试（父子去重、provider 标签、上限）
import { describe, expect, it } from 'vitest';
import {
  OPENLIST_BATCH_LIMIT,
  openlistIsAncestorOrSelf,
  openlistProviderLabel,
} from '../../src/pages/MediaManagementPage';

describe('openlistIsAncestorOrSelf（父子目录判定）', () => {
  it('父目录包含后代', () => {
    expect(openlistIsAncestorOrSelf('/夸克/动画', '/夸克/动画/冰菓')).toBe(true);
    expect(openlistIsAncestorOrSelf('/夸克/动画', '/夸克/动画')).toBe(true);
  });

  it('并列目录互不包含', () => {
    expect(openlistIsAncestorOrSelf('/夸克/动画', '/夸克/真人')).toBe(false);
    expect(openlistIsAncestorOrSelf('/夸克/动画/冰菓', '/夸克/动画/真人')).toBe(false);
  });

  it('同名前缀边界不误判（/动画1 不属于 /动画）', () => {
    expect(openlistIsAncestorOrSelf('/夸克/动画', '/夸克/动画1')).toBe(false);
  });

  it('根路径包含一切', () => {
    expect(openlistIsAncestorOrSelf('/', '/115/动画')).toBe(true);
  });
});

describe('openlistProviderLabel（提供商标签）', () => {
  it('OpenList 导入显示真实 provider，不再统一显示为 OpenList', () => {
    expect(openlistProviderLabel('pan115')).toBe('115 网盘');
    expect(openlistProviderLabel('baidu')).toBe('百度网盘');
    expect(openlistProviderLabel('quark')).toBe('夸克网盘');
    expect(openlistProviderLabel('other')).toBe('其他远程来源');
    expect(openlistProviderLabel('local')).toBe('本地');
  });

  it('缺失或未知 provider 回退为其他远程来源', () => {
    expect(openlistProviderLabel(undefined)).toBe('其他远程来源');
    expect(openlistProviderLabel('')).toBe('其他远程来源');
    // @ts-expect-error 运行时防御
    expect(openlistProviderLabel('aliyun')).toBe('其他远程来源');
  });
});

describe('OPENLIST_BATCH_LIMIT（选择篮上限）', () => {
  it('与后端批量导入上限一致（20）', () => {
    expect(OPENLIST_BATCH_LIMIT).toBe(20);
  });
});

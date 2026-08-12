import { useRef, useState } from 'react';
import { Dropdown, Option } from '@fluentui/react-components';

export type DetailSeasonOption = {
  key: string;
  label: string;
};

type DetailSeasonPickerProps = {
  seasons: DetailSeasonOption[];
  selectedKey: string;
  onSelect: (key: string) => void;
};

function parseChineseSeasonNumber(value: string) {
  const digits: Record<string, number> = { 零: 0, 〇: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  const units: Record<string, number> = { 十: 10, 百: 100, 千: 1000, 万: 10000 };
  let section = 0;
  let current = 0;

  for (const character of value) {
    if (character in digits) {
      current = digits[character];
      continue;
    }
    const unit = units[character];
    if (!unit) return null;
    section += (current || 1) * unit;
    current = 0;
  }

  const result = section + current;
  return result > 0 ? result : null;
}

function normalizeSeasonLabel(label: string) {
  return label
    .replace(/第\s*(\d+)\s*季/g, (_match, digits: string) => `第${digits}季`)
    .replace(/第\s*([零〇一二三四五六七八九十百千万]+)\s*季/g, (_match, chineseNumber: string) => {
      const number = parseChineseSeasonNumber(chineseNumber);
      return number == null ? `第${chineseNumber}季` : `第${number}季`;
    })
    .replace(/\s+/g, ' ')
    .trim();
}

function SeasonLabel({ label, trigger = false }: { label: string; trigger?: boolean }) {
  const className = trigger
    ? 'detail-season-label detail-season-trigger-label'
    : 'detail-season-label detail-season-option-label';
  const visualLabel = normalizeSeasonLabel(label);

  // 季度文字用普通文本节点渲染，与触发器和下拉选项共享同一套字体规则；
  // 不再把每个汉字拆成固定宽度的 glyph，避免出现 650 字重、font-kerning 等不一致。
  return (
    <span className={className} aria-label={label}>
      {visualLabel}
    </span>
  );
}

export default function DetailSeasonPicker({
  seasons,
  selectedKey,
  onSelect,
}: DetailSeasonPickerProps) {
  const selectedSeason = seasons.find((season) => season.key === selectedKey) || seasons[0];
  const selectedLabel = selectedSeason?.label || '选择季度';
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const handleOpenChange = (event: unknown, data: { open: boolean }) => {
    // 触发按钮自身的点击由项目的 onClick 直接切换（函数式更新）；
    // Fluent 内部 onClick 已先调用 setOpen(event, !open) 回传本事件，若再接受 data.open 会双重翻转。
    const ev = event as { type?: string; target?: EventTarget | null } | null;
    const isTriggerClick =
      ev?.type === 'click' &&
      Boolean(triggerRef.current) &&
      Boolean(ev.target) &&
      triggerRef.current!.contains(ev.target as Node);
    if (isTriggerClick) {
      return;
    }
    setOpen(data.open);
  };

  const handleTriggerClick = () => {
    setOpen((current) => !current);
  };

  const handleOptionSelect = (_event: unknown, data: { optionValue?: string }) => {
    if (!data.optionValue) return;
    onSelect(data.optionValue);
    setOpen(false);
  };

  return (
    <Dropdown
      className="detail-season-dropdown"
      aria-label="选择季度"
      size="small"
      open={open}
      onOpenChange={handleOpenChange}
      positioning={{ position: 'above', align: 'start', offset: 6, matchTargetSize: 'width' }}
      button={{
        ref: triggerRef,
        className: 'detail-season-trigger',
        onClick: handleTriggerClick,
        children: <SeasonLabel label={selectedLabel} trigger />,
      }}
      clearButton={null}
      expandIcon={{ className: 'detail-season-chevron', 'aria-hidden': true }}
      listbox={{ className: 'detail-season-listbox', 'aria-label': '可选季度' }}
      value={selectedLabel}
      selectedOptions={selectedKey ? [selectedKey] : []}
      onOptionSelect={handleOptionSelect}
    >
      {seasons.map((season) => (
        <Option key={season.key} value={season.key} text={season.label} checkIcon={null}>
          <SeasonLabel label={season.label} />
        </Option>
      ))}
    </Dropdown>
  );
}

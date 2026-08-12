import { webDarkTheme, webLightTheme, type Theme } from '@fluentui/react-components';

export type KumiAppearanceMode = 'fluent' | 'cinema' | 'mica' | string;

const cinemaTheme: Theme = {
  ...webDarkTheme,
  colorNeutralBackground1: '#292929',
  colorNeutralBackground1Hover: '#3d3d3d',
  colorNeutralBackground1Pressed: '#1f1f1f',
  colorNeutralBackground1Selected: '#383838',
  colorNeutralBackground2: '#1f1f1f',
  colorNeutralBackground2Hover: '#333333',
  colorNeutralBackground2Pressed: '#141414',
  colorNeutralBackground2Selected: '#2e2e2e',
  colorNeutralBackground3: '#141414',
  colorNeutralBackground3Hover: '#292929',
  colorNeutralForeground1: '#f5f5f5',
  colorNeutralForeground2: '#d6d6d6',
  colorNeutralForeground3: '#a3a3a3',
  colorNeutralForegroundOnBrand: '#ffffff',
  colorNeutralStroke1: '#666666',
  colorNeutralStroke2: '#525252',
  colorBrandBackground: '#115ea3',
  colorBrandBackgroundHover: '#0f6cbd',
  colorBrandBackgroundPressed: '#0c3b5e',
  colorBrandBackgroundSelected: '#0f548c',
  colorBrandForeground1: '#479ef5',
  colorBrandForeground2: '#62abf5',
  colorBrandStroke1: '#479ef5',
  colorBrandStroke2: '#0e4775',
  colorCompoundBrandBackground: '#479ef5',
  colorCompoundBrandBackgroundHover: '#62abf5',
  colorCompoundBrandBackgroundPressed: '#2886de',
  colorCompoundBrandForeground1: '#479ef5',
  colorCompoundBrandStroke: '#479ef5',
};

export function getKumiFluentTheme(appearanceMode: KumiAppearanceMode): Theme {
  return appearanceMode === 'cinema' ? cinemaTheme : webLightTheme;
}

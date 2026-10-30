// EXPORTS: IAttraction, MOCK_ATTRACTIONS
export interface IAttraction {
  id: string
  name: string
  description: string
  icon: string
  color: string
}

export const MOCK_ATTRACTIONS: IAttraction[] = [
  {
    id: '1',
    name: '摩天轮',
    description: '俯瞰整个游乐场的浪漫之旅',
    icon: '🎡',
    color: '#FF6B9D',
  },
  {
    id: '2',
    name: '旋转木马',
    description: '梦幻经典的童年欢乐时光',
    icon: '🎠',
    color: '#FFD93D',
  },
  {
    id: '3',
    name: '过山车',
    description: '惊险刺激的极速冒险体验',
    icon: '🎢',
    color: '#6BCFFF',
  },
]
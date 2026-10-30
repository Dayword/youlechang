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
    description: '缓缓升空，俯瞰全园美景',
    icon: '🎡',
    color: '#FF6B9D',
  },
  {
    id: '2',
    name: '旋转木马',
    description: '梦幻旋律，童年美好回忆',
    icon: '🎠',
    color: '#FFD93D',
  },
  {
    id: '3',
    name: '过山车',
    description: '极速冲刺，挑战心跳极限',
    icon: '🎢',
    color: '#6BCFFF',
  },
]
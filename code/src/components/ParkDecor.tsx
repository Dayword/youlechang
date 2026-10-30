import { motion } from 'framer-motion';
import { memo } from 'react';

// 游乐场风格背景装饰：气球、云朵、星星、彩旗
function ParkDecor() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* 左上气球组 */}
      <motion.div
        className="absolute left-[8%] top-[10%] text-6xl md:text-7xl"
        animate={{ y: [0, -16, 0] }}
        transition={{
          duration: 4,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        🎈
      </motion.div>
      <motion.div
        className="absolute left-[14%] top-[18%] text-5xl md:text-6xl"
        animate={{ y: [0, -12, 0] }}
        transition={{
          duration: 5,
          repeat: Infinity,
          ease: 'easeInOut',
          delay: 0.8,
        }}
      >
        🎈
      </motion.div>

      {/* 右上云朵 + 星星 */}
      <motion.div
        className="absolute right-[10%] top-[8%] text-5xl md:text-6xl"
        animate={{ y: [0, -8, 0] }}
        transition={{
          duration: 6,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        ☁️
      </motion.div>
      <motion.div
        className="absolute right-[20%] top-[14%] text-3xl md:text-4xl"
        animate={{ rotate: [0, 15, -10, 0], scale: [1, 1.1, 1] }}
        transition={{
          duration: 3,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        ⭐
      </motion.div>

      {/* 左下糖果 / 糖果 */}
      <motion.div
        className="absolute bottom-[12%] left-[6%] text-5xl md:text-6xl"
        animate={{ rotate: [-8, 8, -8], y: [0, -6, 0] }}
        transition={{
          duration: 4,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        🍭
      </motion.div>

      {/* 右下旋转图标 */}
      <motion.div
        className="absolute bottom-[10%] right-[8%] text-6xl md:text-7xl"
        animate={{ rotate: 360 }}
        transition={{
          duration: 20,
          repeat: Infinity,
          ease: 'linear',
        }}
      >
        🎡
      </motion.div>

      {/* 彩旗串 */}
      <motion.div
        className="absolute left-1/2 top-[4%] -translate-x-1/2 text-3xl md:text-4xl tracking-widest"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.4, duration: 0.8 }}
      >
        🎉🎊🎉🎊🎉
      </motion.div>

      {/* 中间偏右旋转木马 */}
      <motion.div
        className="absolute right-[4%] top-[45%] text-5xl md:text-6xl"
        animate={{ y: [0, -10, 0], scale: [1, 1.05, 1] }}
        transition={{
          duration: 3.5,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        🎠
      </motion.div>

      {/* 左下星星 */}
      <motion.div
        className="absolute bottom-[30%] left-[3%] text-2xl md:text-3xl"
        animate={{ scale: [1, 1.3, 1], opacity: [0.6, 1, 0.6] }}
        transition={{
          duration: 2,
          repeat: Infinity,
          ease: 'easeInOut',
        }}
      >
        ✨
      </motion.div>
    </div>
  );
}

export default memo(ParkDecor);

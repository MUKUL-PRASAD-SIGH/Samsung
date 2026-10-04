/** The product identity: Kairos (Καιρός), the Greek word for the right, opportune moment: acting at the right time. */
export function Logo({ className = 'w-8 h-8' }) {
  return <img src="/kairos.svg" alt="Kairos" className={className} draggable={false} />;
}

export function Wordmark({ size = 'lg' }) {
  const big = size === 'lg';
  return (
    <div className="flex flex-col items-center select-none">
      <h1 className={`${big ? 'text-6xl md:text-7xl' : 'text-xl'} font-semibold tracking-[0.18em] bg-gradient-to-r from-amber-200 via-amber-300 to-amber-500 bg-clip-text text-transparent`}
          style={{ fontFamily: '"Cormorant Garamond", "Palatino Linotype", Georgia, serif' }}>
        KAIROS
      </h1>
      <div className={`${big ? 'text-lg mt-1' : 'text-[11px]'} tracking-[0.35em] text-amber-200/60`}
           style={{ fontFamily: '"Cormorant Garamond", Georgia, serif' }}>
        ΚΑΙΡΟΣ
      </div>
      {big && <p className="text-sm text-slate-400 mt-3">The right moment to act. A real-time agent you can interrupt.</p>}
    </div>
  );
}

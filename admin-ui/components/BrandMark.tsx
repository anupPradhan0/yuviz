/* eslint-disable @next/next/no-img-element -- static .ico; next/image can't optimise it */
export function BrandMark({ size = 24, className }: { size?: number; className?: string }) {
  return <img src="/favicon.ico" alt="" aria-hidden="true" width={size} height={size} className={className} />;
}

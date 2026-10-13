/* eslint-disable @next/next/no-img-element -- tiny static logo; next/image adds nothing here */
export function BrandMark({ size = 24, className }: { size?: number; className?: string }) {
  return <img src="/brand-mark.png" alt="" aria-hidden="true" width={size} height={size} className={className} />;
}

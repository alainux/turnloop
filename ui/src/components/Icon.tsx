import type { ImgHTMLAttributes } from "react";
export function Icon({
  name,
  ...props
}: { name: string } & Omit<ImgHTMLAttributes<HTMLImageElement>, "src">) {
  return (
    <img
      className={`icon ${props.className ?? ""}`}
      src={`/icons/${name}.svg`}
      alt=""
      aria-hidden="true"
      {...props}
    />
  );
}

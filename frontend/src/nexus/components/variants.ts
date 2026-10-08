import { cva } from "class-variance-authority";

export const nxButton = cva(
  "inline-flex shrink-0 cursor-pointer items-center justify-center gap-2 whitespace-nowrap font-medium transition-all duration-200 disabled:pointer-events-none disabled:opacity-40 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary:
          "bg-gradient-to-r from-cyan via-electric to-violet text-white shadow-[0_8px_30px_-8px_rgb(34_211_238/0.55)] hover:shadow-[0_10px_40px_-6px_rgb(59_130_246/0.7)] hover:brightness-110 active:scale-[0.98]",
        glass: "nx-glass text-mist hover:border-line-strong hover:bg-glass-strong hover:text-frost",
        ghost: "text-haze hover:bg-glass-strong hover:text-frost",
        danger: "bg-rose/15 text-rose ring-1 ring-rose/30 hover:bg-rose/25",
      },
      size: {
        sm: "h-8 rounded-lg px-3 text-[13px] [&_svg]:size-4",
        md: "h-10 rounded-xl px-4 text-sm [&_svg]:size-4",
        lg: "h-12 rounded-2xl px-6 text-[15px] [&_svg]:size-5",
        icon: "size-10 rounded-xl [&_svg]:size-[18px]",
        "icon-lg": "size-14 rounded-full [&_svg]:size-6",
      },
    },
    defaultVariants: { variant: "glass", size: "md" },
  },
);

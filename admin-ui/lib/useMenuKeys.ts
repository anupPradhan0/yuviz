import { useEffect, useRef } from "react";

// role="menu" keyboard contract: focus the first item on open, Escape/Tab close,
// arrows/Home/End move between items, and focus returns to the trigger on close.
export function useMenuKeys(open: boolean, close: () => void) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(close);
  useEffect(() => {
    closeRef.current = close;
  });

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    window.addEventListener("keydown", onKey);
    const trigger = triggerRef.current;
    menuRef.current?.querySelector<HTMLElement>("[role=menuitem]")?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      // The focused item was just unmounted; hand focus back unless something else took it.
      if (document.activeElement === document.body) trigger?.focus();
    };
  }, [open]);

  const onMenuKey = (e: React.KeyboardEvent) => {
    if (e.key === "Tab") return closeRef.current();
    const items = [...(menuRef.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next = ({ ArrowDown: at + 1, ArrowUp: at - 1 + items.length, Home: 0, End: items.length - 1 } as Record<string, number>)[e.key];
    if (next === undefined) return;
    e.preventDefault();
    items[next % items.length]?.focus();
  };

  return { triggerRef, menuRef, onMenuKey };
}

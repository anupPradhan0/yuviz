"use client";

// The Config Service's free-model catalogue and download state, shared by the agent pages.
// Polls while any model in `selected` is downloading, so a badge flips to ready without a reload.

import { useCallback, useEffect, useMemo, useState } from "react";
import { LocalModel, LocalModelStatus, listLocalModels, triggerModelDownload } from "@/lib/api";

const POLL_MS = 3000;

export interface ModelRef {
  engine: string;
  name: string;
}

const keyOf = (engine: string, name: string) => `${engine}\u0000${name}`;

export function useLocalModels(selected: ModelRef[] = []) {
  const [models, setModels] = useState<LocalModel[]>([]);
  const [error, setError] = useState<string | null>(null);

  const byKey = useMemo(() => new Map(models.map((m) => [keyOf(m.engine, m.name), m])), [models]);

  const refresh = useCallback(
    () =>
      listLocalModels()
        .then((m) => {
          setModels(m);
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const anyDownloading = selected.some((s) => byKey.get(keyOf(s.engine, s.name))?.status === "downloading");
  useEffect(() => {
    if (!anyDownloading) return;
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [anyDownloading, refresh]);

  // undefined = not a catalogue model (cloud engine or custom name), so no badge applies.
  const status = (engine: string, name: string): LocalModelStatus | undefined =>
    byKey.get(keyOf(engine, name))?.status;

  const trigger = async (engine: string, name: string) => {
    await triggerModelDownload(engine, name);
    await refresh();
  };

  // A model with no language list (LLMs, unknown names) is not language-bound.
  const supports = (engine: string, name: string, lang: string): boolean => {
    const languages = byKey.get(keyOf(engine, name))?.languages;
    return languages ? languages.includes(lang) : true;
  };

  return { models, error, status, trigger, supports };
}

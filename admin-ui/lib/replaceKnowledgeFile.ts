import type { KbDocument, DocumentUpdate } from "./knowledgeApi";

type ReplacementApi = {
  uploadDocument: (
    kbId: string,
    file: File,
    title: string,
    options: { language: string | null; tags: Record<string, unknown> },
  ) => Promise<KbDocument>;
  listDocuments: (kbId: string) => Promise<KbDocument[]>;
  updateDocument: (id: string, update: DocumentUpdate) => Promise<KbDocument>;
  deleteDocument: (id: string) => Promise<void>;
};

/** A successful upload only queues preparation; it does not make a safe replacement. */
export async function replaceKnowledgeFile(
  original: KbDocument,
  file: File,
  api: ReplacementApi,
  options: {
    isCurrent?: () => boolean;
    wait?: () => Promise<void>;
    checks?: number;
  } = {},
): Promise<void> {
  const isCurrent = options.isCurrent ?? (() => true);
  const wait =
    options.wait ??
    (() => new Promise<void>((resolve) => setTimeout(resolve, 2000)));
  let replacement: KbDocument | null = null;
  let originalRemovalStarted = false;
  try {
    if (!isCurrent())
      throw new Error(
        "The folder or account changed before replacement started.",
      );
    replacement = await api.uploadDocument(
      original.kb_id,
      file,
      original.title,
      {
        language: original.language,
        tags: original.tags,
      },
    );
    for (
      let check = 0;
      replacement.status === "pending" || replacement.status === "processing";
      check++
    ) {
      if (!isCurrent())
        throw new Error(
          "The folder or account changed before replacement finished.",
        );
      if (check >= (options.checks ?? 15)) {
        throw new Error(
          "The replacement took too long to prepare.",
        );
      }
      await wait();
      if (!isCurrent())
        throw new Error(
          "The folder or account changed before replacement finished.",
        );
      const replacementId: string = replacement.id;
      const current: KbDocument | undefined = (await api.listDocuments(original.kb_id)).find(
        (doc) => doc.id === replacementId,
      );
      if (!current) throw new Error("The replacement could not be found.");
      replacement = current;
    }
    if (!isCurrent())
      throw new Error(
        "The folder or account changed before replacement finished.",
      );
    if (replacement.status !== "ready") {
      throw new Error(
        "The replacement could not be fully prepared.",
      );
    }
    // Preserve always-read behaviour only after preparation has succeeded.
    if (original.usage_mode !== replacement.usage_mode) {
      await api.updateDocument(replacement.id, {
        usage_mode: original.usage_mode,
      });
    }
    if (!isCurrent())
      throw new Error(
        "The folder or account changed before replacement finished.",
      );
    try {
      originalRemovalStarted = true;
      await api.deleteDocument(original.id);
    } catch {
      // A lost response cannot tell us whether DELETE reached the server.
      throw new Error(
        "The replacement is ready, but removal of the original could not be confirmed. Check the file list before removing either copy.",
      );
    }
  } catch (error) {
    if (!replacement || originalRemovalStarted) throw error;
    const detail = error instanceof Error ? error.message : String(error);
    // Pending and degraded uploads can later be retrieved alongside the original.
    // Roll back only while we know the original has never been removed.
    if (replacement.status !== "failed") {
      try {
        await api.deleteDocument(replacement.id);
      } catch {
        throw new Error(
          `${detail} The original file has been kept, but removal of the new file could not be confirmed. Both copies may be available to agents. Check the file list and remove the new file before trying again.`,
        );
      }
      throw new Error(
        `${detail} The original file has been kept. The new file was discarded; try again.`,
      );
    }
    throw new Error(
      `${detail} The original file has been kept. The new file failed preparation; check or remove it in the file list.`,
    );
  }
}

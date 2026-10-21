"use client";

import { createContext } from "react";

// The numbers page shares its loaded connection permissions with the top bar.
export const PhoneNumberActionContext = createContext<((enabled: boolean) => void) | null>(null);

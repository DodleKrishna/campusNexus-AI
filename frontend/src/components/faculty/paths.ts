import type { FacultyClass } from "@/types/api";

/** The class workspace route for one class meeting. */
export const classPath = (c: Pick<FacultyClass, "session_id">) => `/faculty/classes/${c.session_id}`;

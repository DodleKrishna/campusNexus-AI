import { AcademicAnswer } from "@/features/academic/AcademicAnswer";
import { ComplaintsAnswer } from "@/features/complaints/ComplaintsAnswer";
import { EventsAnswer } from "@/features/events/EventsAnswer";
import { PlacementsAnswer } from "@/features/placements/PlacementsAnswer";
import { AnswerText } from "@/features/shared";
import type { SpecialistAnswer } from "@/types/api";

/** Structured cards for one specialist's facts; one renderer per agent, shared by chat and the Enquiry Agent. */
export function SpecialistFacts({ answer }: { answer: Pick<SpecialistAnswer, "agent_key" | "facts" | "evidence" | "answer"> }) {
  switch (answer.agent_key) {
    case "academic":
      return <AcademicAnswer facts={answer.facts} evidence={answer.evidence} answer={answer.answer} />;
    case "events":
      return <EventsAnswer facts={answer.facts} answer={answer.answer} />;
    case "placements":
      return <PlacementsAnswer facts={answer.facts} answer={answer.answer} />;
    case "complaints":
      return <ComplaintsAnswer facts={answer.facts} answer={answer.answer} />;
    default:
      return <AnswerText text={answer.answer} />;
  }
}

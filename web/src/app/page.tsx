import { Suspense } from "react";
import { AskPage } from "@/components/ask/ask-page";

export default function Page() {
  return (
    <Suspense>
      <AskPage />
    </Suspense>
  );
}

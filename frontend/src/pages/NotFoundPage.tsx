import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center text-center">
      <p className="text-sm font-semibold text-accent-hover">404</p>
      <h1 className="mt-2 text-xl font-semibold">This page doesn't exist</h1>
      <Link to="/" className="mt-4 text-sm font-medium text-accent-hover">
        Go to your workspace
      </Link>
    </div>
  );
}

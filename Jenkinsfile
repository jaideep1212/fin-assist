// Jenkinsfile — CI pipeline for fin-assist.
// Jenkins builds the project's Docker image, then runs the tests inside a
// fresh, throwaway container. Jenkins never runs Python itself; it only
// orchestrates Docker (Option A).

pipeline {
    agent any

    stages {
        stage('Build image') {
            steps {
                echo 'Building the fin-assist image from the checked-out code...'
                sh 'docker build -t fin-assist:ci .'
            }
        }

        stage('Run tests') {
            steps {
                echo 'Running pytest inside a throwaway container...'
                sh 'docker run --rm fin-assist:ci pytest -q'
            }
        }
    }

    post {
        success {
            echo 'All tests passed. Pipeline is green.'
        }
        failure {
            echo 'Pipeline failed. Check the console output above for the reason.'
        }
    }
}